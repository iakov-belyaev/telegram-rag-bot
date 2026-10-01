import asyncio
import os
import uuid
import re
from dotenv import load_dotenv
from fastembed import TextEmbedding
from openai import OpenAI
from pyrogram import Client, filters, errors
from pyrogram.types import InlineQueryResultArticle, InputTextMessageContent
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct


load_dotenv()

# --- Configuration & Limits ---
MESSAGES_COLLECTION = "telegram_messages"
CHATS_COLLECTION = "telegram_chats"
MAX_POINTS_WARN_THRESHOLD = 800_000  # Qdrant free tier ~1M vector capacity warning limit
STORAGE_CHECK_INTERVAL_SECONDS = 3600  # Check DB capacity every hour

print("🚀 Initializing components...")

# 1. Load Local Embedding Model (384 Dimensions)
embedding_model = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")

# 2. Connect to Qdrant Cloud
qdrant = QdrantClient(
    url=os.getenv("QDRANT_URL"),
    api_key=os.getenv("QDRANT_API_KEY")
)

# 3. DeepSeek API Client
deepseek_client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com"
)

# 4. Pyrogram Userbot Client (Ingestion & Catch-up)
userbot = Client(
    "my_userbot",
    api_id=int(os.getenv("TELEGRAM_API_ID")),
    api_hash=os.getenv("TELEGRAM_API_HASH")
)

# 5. Pyrogram Inline Bot Client (Search & Query Interface)
inline_bot = Client(
    "inline_bot",
    api_id=int(os.getenv("TELEGRAM_API_ID")),
    api_hash=os.getenv("TELEGRAM_API_HASH"),
    bot_token=os.getenv("BOT_TOKEN")
)

# Global holder for admin ID
ADMIN_USER_ID = None
alert_sent = False

# ==========================================
# 1. REAL-TIME USERBOT LISTENER
# ==========================================
@userbot.on_message(filters.text | filters.caption)
async def process_incoming_message(client, message):
    try:
        # 1. Extract text or caption
        raw_text = message.text or message.caption or ""
        text = raw_text.strip()
        if len(text) < 10:
            return

        chat = message.chat
        chat_title = chat.title or chat.first_name or "Private Chat"
        chat_username = chat.username or ""

        # 2. Safely extract sender details and username
        if message.from_user:
            sender_id = message.from_user.id
            first_name = message.from_user.first_name or ""
            last_name = message.from_user.last_name or ""
            sender_name = f"{first_name} {last_name}".strip() or "Unknown"
            sender_username = message.from_user.username or ""
        else:
            sender_id = chat.id
            sender_name = chat_title
            sender_username = chat_username

        # 3. Format Telegram deep-link correctly
        if chat_username:
            message_link = f"https://t.me/{chat_username}/{message.id}"
        else:
            # Strip '-100' prefix from private supergroup/channel IDs for web/app deep-linking
            clean_chat_id = str(chat.id).replace("-100", "").replace("-", "")
            message_link = f"https://t.me/c/{clean_chat_id}/{message.id}"

        # 4. Upsert Chat Metadata (Vectorized so CHATS_COLLECTION is semantically searchable)
        chat_point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"chat_{chat.id}"))
        chat_embed_text = f"Group: {chat_title}" + (f" (@{chat_username})" if chat_username else "")
        chat_embedding = list(embedding_model.embed([chat_embed_text]))[0].tolist()

        qdrant.upsert(
            collection_name=CHATS_COLLECTION,
            points=[
                PointStruct(
                    id=chat_point_id,
                    vector=chat_embedding,
                    payload={
                        "chat_id": chat.id,
                        "chat_title": chat_title,
                        "chat_type": str(chat.type),
                        "username": chat_username,
                    }
                )
            ]
        )

        # 5. Embed message and store full payload
        msg_embedding = list(embedding_model.embed([text]))[0].tolist()
        msg_point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{chat.id}_{message.id}"))

        msg_payload = {
            "message_id": message.id,
            "chat_id": chat.id,
            "chat_title": chat_title,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "sender_username": sender_username,
            "text": text,
            "date": str(message.date),
            "link": message_link
        }

        qdrant.upsert(
            collection_name=MESSAGES_COLLECTION,
            points=[
                PointStruct(
                    id=msg_point_id, 
                    vector=msg_embedding, 
                    payload=msg_payload
                )
            ]
        )

    except Exception as e:
        print(f"Error indexing live message: {e}")

# ==========================================
# 2. UNIVERSAL NATURAL LANGUAGE INTELLIGENCE BOT
# ==========================================
@inline_bot.on_inline_query()
async def answer_inline_query(client, inline_query):
    query_text = inline_query.query.strip()
    if not query_text:
        return

    try:
        # 1. Embed user query
        query_vector = list(embedding_model.embed([query_text]))[0].tolist()

        # 2. Retrieve relevant message history
        msg_response = qdrant.query_points(
            collection_name=MESSAGES_COLLECTION,
            query=query_vector,
            limit=15
        )
        matched_messages = msg_response.points

        # 3. Retrieve relevant group/channel metadata
        chat_response = qdrant.query_points(
            collection_name=CHATS_COLLECTION,
            query=query_vector,
            limit=10
        )
        matched_chats = chat_response.points

        if not matched_messages and not matched_chats:
            return

        # 4. Build Context Blocks
        chat_context_lines = []
        for chat_hit in matched_chats:
            cp = chat_hit.payload
            chat_context_lines.append(
                f"- Chat Title: '{cp.get('chat_title')}' | Type: {cp.get('chat_type')} | Username: @{cp.get('username') or 'N/A'}"
            )

        msg_context_lines = []
        for msg_hit in matched_messages:
            mp = msg_hit.payload
            user_str = f"{mp.get('sender_name')}" + (f" (@{mp.get('sender_username')})" if mp.get('sender_username') else "")
            msg_context_lines.append(
                f"- [{user_str} in '{mp.get('chat_title')}']: {mp.get('text')}"
            )

        context_str = (
            "=== KNOWN GROUPS & CHANNELS DIRECTORY ===\n"
            + ("\n".join(chat_context_lines) if chat_context_lines else "None")
            + "\n\n=== CHAT MESSAGE LOGS ===\n"
            + ("\n".join(msg_context_lines) if msg_context_lines else "None")
        )

        # 5. Universal Prompt for Any Natural Language Request
        system_prompt = (
            "You are an advanced Telegram Intelligence Assistant. Your job is to answer user queries based STRICTLY on the provided Telegram database context.\n\n"
            "Adapt your response automatically based on the user's intent:\n"
            "1. **Group/Channel Lookup** (e.g., 'list groups for @user', 'animal groups'): Extract and bullet-list all relevant chats/channels where the entity participated or that match the topic.\n"
            "2. **User Profile / Dossier** (e.g., 'who is @user', 'tell me about X'): Synthesize a brief dossier on their background, role, and topics they discuss.\n"
            "3. **General Q&A / Search**: Answer the question directly and concisely.\n\n"
            "Formatting Rules:\n"
            "- Keep answers clear, professional, and well-structured with Markdown bullet points.\n"
            "- Respond in the same language as the user query (Russian or English).\n"
            "- Never fabricate facts not found in the context."
        )

        # 6. Synthesize with DeepSeek
        ai_response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Database Context:\n{context_str}\n\nUser Command: {query_text}"}
            ],
            max_tokens=450
        )
        answer_text = ai_response.choices[0].message.content

        # 7. Render as a single clean interactive card
        handles = re.findall(r"@([a-zA-Z0-9_]+)", query_text)
        card_title = f"🔎 Intelligence Report: {query_text[:30]}..."

        results = [
            InlineQueryResultArticle(
                title=card_title,
                description=answer_text[:90].replace("\n", " ") + "...",
                input_message_content=InputTextMessageContent(
                    f"**Command:** `{query_text}`\n\n{answer_text}"
                )
            )
        ]

        await inline_query.answer(results, cache_time=1)

    except Exception as e:
        print(f"Error handling inline query: {e}")


# ==========================================
# 3. BACKGROUND CATCH-UP SCRAPER
# ==========================================
async def historical_catchup():
    print("🔄 Running background catch-up sync for recent missed messages...")
    try:
        async for dialog in userbot.get_dialogs():
            try:
                chat = dialog.chat
                chat_title = chat.title or chat.first_name or "Private Chat"
                chat_username = chat.username or ""

                # 1. Upsert Chat Metadata into CHATS_COLLECTION (vectorized)
                chat_point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"chat_{chat.id}"))
                chat_embed_text = f"Group: {chat_title}" + (f" (@{chat_username})" if chat_username else "")
                chat_embedding = list(embedding_model.embed([chat_embed_text]))[0].tolist()

                qdrant.upsert(
                    collection_name=CHATS_COLLECTION,
                    points=[
                        PointStruct(
                            id=chat_point_id,
                            vector=chat_embedding,
                            payload={
                                "chat_id": chat.id,
                                "chat_title": chat_title,
                                "chat_type": str(chat.type),
                                "username": chat_username,
                            }
                        )
                    ]
                )

                # 2. Fetch recent history (extract text or media captions)
                messages_to_process = []
                async for message in userbot.get_chat_history(chat.id, limit=100):
                    raw_text = message.text or message.caption or ""
                    clean_text = raw_text.strip()
                    if len(clean_text) >= 10:
                        messages_to_process.append((message, clean_text))

                if not messages_to_process:
                    continue

                texts = [item[1] for item in messages_to_process]
                embeddings = list(embedding_model.embed(texts))

                points = []
                for (message, text), embedding in zip(messages_to_process, embeddings):
                    # Sender extraction with full name and username handle
                    if message.from_user:
                        sender_id = message.from_user.id
                        first_name = message.from_user.first_name or ""
                        last_name = message.from_user.last_name or ""
                        sender_name = f"{first_name} {last_name}".strip() or "Unknown"
                        sender_username = message.from_user.username or ""
                    else:
                        sender_id = chat.id
                        sender_name = chat_title
                        sender_username = chat_username

                    # Clean deep-link generation
                    if chat_username:
                        message_link = f"https://t.me/{chat_username}/{message.id}"
                    else:
                        clean_chat_id = str(chat.id).replace("-100", "").replace("-", "")
                        message_link = f"https://t.me/c/{clean_chat_id}/{message.id}"

                    point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{chat.id}_{message.id}"))

                    points.append(
                        PointStruct(
                            id=point_id,
                            vector=embedding.tolist(),
                            payload={
                                "message_id": message.id,
                                "chat_id": chat.id,
                                "chat_title": chat_title,
                                "sender_id": sender_id,
                                "sender_name": sender_name,
                                "sender_username": sender_username,
                                "text": text,
                                "date": str(message.date),
                                "link": message_link
                            }
                        )
                    )

                qdrant.upsert(collection_name=MESSAGES_COLLECTION, points=points)
                await asyncio.sleep(0.2)

            except errors.FloodWait as e:
                print(f"⚠️ FloodWait encountered for chat {dialog.chat.id}, sleeping {e.value}s...")
                await asyncio.sleep(e.value)
            except Exception as e:
                print(f"Error syncing history for chat {dialog.chat.id}: {e}")

    except Exception as e:
        print(f"Catch-up task error: {e}")

    print("✅ Background historical catch-up sync complete.")

# ==========================================
# 4. CAPACITY & OVERFILL NOTIFIER
# ==========================================
async def monitor_qdrant_capacity():
    global alert_sent
    while True:
        try:
            collection_info = qdrant.get_collection(MESSAGES_COLLECTION)
            points_count = collection_info.points_count or 0

            print(f"📊 Storage Check: Currently holding {points_count:,} vectors in Qdrant.")

            if points_count >= MAX_POINTS_WARN_THRESHOLD and not alert_sent:
                alert_text = (
                    f"⚠️ **QDRANT STORAGE WARNING** ⚠️️\n\n"
                    f"Your database is approaching its capacity limit!\n"
                    f"Current Indexed Messages: `{points_count:,}`\n"
                    f"Threshold Warning Level: `{MAX_POINTS_WARN_THRESHOLD:,}`\n\n"
                    f"Please purge old messages or upgrade your Qdrant cluster."
                )
                if ADMIN_USER_ID:
                    try:
                        await inline_bot.send_message(chat_id=ADMIN_USER_ID, text=alert_text)
                        print(f"🔔 Overfill alert dispatched to user ID {ADMIN_USER_ID}")
                        alert_sent = True
                    except errors.PeerIdInvalid:
                        print(f"⚠️ Storage alert triggered ({points_count:,} points), but bot cannot message user {ADMIN_USER_ID} until you click /start in Telegram.")

            elif points_count < MAX_POINTS_WARN_THRESHOLD and alert_sent:
                alert_sent = False

        except Exception as e:
            print(f"Error checking Qdrant capacity: {e}")

        await asyncio.sleep(STORAGE_CHECK_INTERVAL_SECONDS)


# ==========================================
# MAIN RUNNER
# ==========================================
async def main():
    global ADMIN_USER_ID
    
    print("Starting Telegram Userbot and Inline Bot...")
    await userbot.start()
    await inline_bot.start()

    me = await userbot.get_me()
    bot_me = await inline_bot.get_me()

    ADMIN_USER_ID = me.id
    target_bot_username = os.getenv("BOT_USERNAME", f"@{bot_me.username}")

    print(f"✅ Logged in as User: {me.first_name} (ID: {ADMIN_USER_ID})")
    print(f"🤖 Bot active: {target_bot_username}")

    try:
        await inline_bot.send_message(
            chat_id=ADMIN_USER_ID,
            text="🚀 **Telegram Scraper & Vector Search Engine Started**\nReal-time ingestion and capacity monitoring are now active."
        )
        print("📩 Bootup notification sent to your Telegram DM!")
    except errors.PeerIdInvalid:
        print(
            f"\n⚠️ ACTION REQUIRED:\n"
            f"   Open Telegram, search for {target_bot_username}, and click /start.\n"
            f"   The engine is running, but the bot needs you to message it first to send alerts.\n"
        )
    except Exception as e:
        print(f"Notice: Could not send startup message: {e}")

    asyncio.create_task(historical_catchup())
    asyncio.create_task(monitor_qdrant_capacity())

    print("🟢 All services online. Listening continuously...")
    await asyncio.Event().wait()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nShutting down engine...")