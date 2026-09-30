import asyncio
import os
import uuid
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
@userbot.on_message(filters.text)
async def process_incoming_message(client, message):
    try:
        if not message.text or len(message.text.strip()) < 10:
            return

        chat = message.chat
        chat_title = chat.title or chat.first_name or "Private Chat"
        text = message.text.strip()

        # Update group metadata
        chat_point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"chat_{chat.id}"))
        qdrant.upsert(
            collection_name=CHATS_COLLECTION,
            points=[
                PointStruct(
                    id=chat_point_id,
                    vector=[0.0],
                    payload={
                        "chat_id": chat.id,
                        "chat_title": chat_title,
                        "chat_type": str(chat.type),
                        "username": chat.username or "",
                    }
                )
            ]
        )

        # Vectorize and insert message
        embedding = list(embedding_model.embed([text]))[0]
        sender_id = message.from_user.id if message.from_user else chat.id
        sender_name = message.from_user.first_name if message.from_user else "Unknown"
        message_link = (
            message.link 
            if hasattr(message, "link") and message.link 
            else f"https://t.me/c/{abs(chat.id)}/{message.id}"
        )

        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{chat.id}_{message.id}"))

        payload = {
            "message_id": message.id,
            "chat_id": chat.id,
            "chat_title": chat_title,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "text": text,
            "date": str(message.date),
            "link": message_link
        }

        qdrant.upsert(
            collection_name=MESSAGES_COLLECTION,
            points=[PointStruct(id=point_id, vector=embedding.tolist(), payload=payload)]
        )

    except Exception as e:
        print(f"Error indexing live message: {e}")


# ==========================================
# 2. INLINE SEARCH & DEEPSEEK RAG BOT
# ==========================================
@inline_bot.on_inline_query()
async def answer_inline_query(client, inline_query):
    query_text = inline_query.query.strip()
    if not query_text:
        return

    try:
        query_vector = list(embedding_model.embed([query_text]))[0].tolist()

        search_results = qdrant.search(
            collection_name=MESSAGES_COLLECTION,
            query_vector=query_vector,
            limit=5
        )

        if not search_results:
            return

        results = []
        context_lines = [
            f"- [{hit.payload['sender_name']} in {hit.payload['chat_title']}]: {hit.payload['text']}"
            for hit in search_results
        ]
        context_str = "\n".join(context_lines)

        try:
            ai_response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "You are a concise assistant summarizing facts retrieved from Telegram chat history. Answer accurately using only context provided."},
                    {"role": "user", "content": f"Context messages:\n{context_str}\n\nQuestion: {query_text}"}
                ],
                max_tokens=250
            )
            rag_answer = ai_response.choices[0].message.content
        except Exception as e:
            rag_answer = f"Could not generate AI summary: {e}"

        results.append(
            InlineQueryResultArticle(
                title="🤖 DeepSeek AI Summary",
                description=rag_answer[:100] + "...",
                input_message_content=InputTextMessageContent(
                    f"**🤖 Answer for:** *\"{query_text}\"*\n\n{rag_answer}\n\n*(Synthesized from {len(search_results)} messages)*"
                )
            )
        )

        for hit in search_results:
            p = hit.payload
            results.append(
                InlineQueryResultArticle(
                    title=f"💬 {p['sender_name']} in {p['chat_title']}",
                    description=p['text'][:100],
                    input_message_content=InputTextMessageContent(
                        f"**Sender:** {p['sender_name']}\n**Chat:** {p['chat_title']}\n**Date:** {p['date']}\n\n💬 *\"{p['text']}\"*\n\n🔗 [Jump to message]({p['link']})"
                    )
                )
            )

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
            chat = dialog.chat
            chat_title = chat.title or chat.first_name or "Private Chat"
            
            messages_to_process = []
            async for message in userbot.get_chat_history(chat.id, limit=100):
                if message.text and len(message.text.strip()) >= 10:
                    messages_to_process.append(message)

            if not messages_to_process:
                continue

            texts = [m.text.strip() for m in messages_to_process]
            embeddings = list(embedding_model.embed(texts))

            points = []
            for message, embedding in zip(messages_to_process, embeddings):
                sender_id = message.from_user.id if message.from_user else chat.id
                sender_name = message.from_user.first_name if message.from_user else "Unknown"
                message_link = message.link if hasattr(message, "link") and message.link else f"https://t.me/c/{abs(chat.id)}/{message.id}"
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
                            "text": message.text.strip(),
                            "date": str(message.date),
                            "link": message_link
                        }
                    )
                )

            qdrant.upsert(collection_name=MESSAGES_COLLECTION, points=points)
            await asyncio.sleep(0.2)
    except errors.FloodWait as e:
        await asyncio.sleep(e.value)
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