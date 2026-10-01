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
MAX_VECTORS = int(os.getenv("MAX_VECTORS", "100000"))  # Hard capacity ceiling for the messages collection
STORAGE_WARN_RATIO = 0.80   # Send a DM notification at 80% capacity
STORAGE_PAUSE_RATIO = 0.95  # Pause background scraping at 95% capacity
STORAGE_CHECK_INTERVAL_SECONDS = 3600  # Check DB capacity every hour
SCRAPER_IDLE_SLEEP_SECONDS = 300  # Sleep between full deep-scrape passes
SCRAPER_PAGE_SIZE = 100  # Messages fetched per history page

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

# --- Noise channel filtering ---
# Regex patterns matched (case-insensitive) against chat title + username.
# Any match marks the chat as low-value noise and it is skipped by the scraper.
NOISE_TITLE_PATTERNS = [
    r"\bnsfw\b", r"\bxxx\b", r"\bporn", r"\bsex\b", r"\bhentai\b", r"\b18\+",
    r"\badult\b", r"\berotic\b", r"\bnude", r"\bonlyfans\b",
    r"\bmeme", r"\bмем", r"\bприкол", r"\bjoke", r"\bfunny\b", r"\bhumor\b",
    r"\bgambl", r"\bcasino\b", r"\bbet\b", r"\bbetting\b", r"\bказино\b",
    r"\bsticker", r"\bстикер", r"\bemoji", r"\bсмайл",
    r"\bairdrop\b", r"\bcrypto\s*signal", r"\bpump\b", r"\bscam\b",
]
NOISE_TITLE_REGEX = re.compile("|".join(NOISE_TITLE_PATTERNS), re.IGNORECASE)

# Minimum fraction of text-bearing messages required to consider a chat high-value.
# Chats dominated by media/stickers with little text are treated as noise.
MIN_TEXT_RATIO = 0.30
# Number of recent messages sampled to compute the text-to-media ratio.
FILTER_SAMPLE_SIZE = 30
# Minimum number of sampled messages required before trusting the ratio.
FILTER_MIN_SAMPLES = 10

# Set to True by StorageShield when capacity crosses the pause threshold.
# Background scrapers check this flag and stop ingesting new points.
scraping_paused = False


def build_message_link(chat, message_id):
    """Build a correct Telegram deep-link for private chats, groups, and channels."""
    if getattr(chat, "username", None):
        return f"https://t.me/{chat.username}/{message_id}"
    # Strip '-100' prefix from supergroup/channel IDs for web/app deep-linking
    clean_chat_id = str(chat.id).replace("-100", "").replace("-", "")
    return f"https://t.me/c/{clean_chat_id}/{message_id}"


async def embed_texts(texts):
    """Run the synchronous FastEmbed model in a worker thread to avoid blocking the event loop."""
    return await asyncio.to_thread(lambda: list(embedding_model.embed(texts)))


async def is_high_value_chat(chat):
    """Decide whether a chat is worth scraping.

    Returns True for high-value chats, False for noise (NSFW, porn, memes,
    gambling, sticker-only, or broadcast-only channels).

    Heuristics:
      1. Title/username regex match against known noise keywords.
      2. Broadcast-only channels (no meaningful sender variety) are skipped.
      3. Text-to-media ratio over recent history: chats dominated by
         stickers/media with little text are treated as noise.
    """
    chat_title = chat.title or chat.first_name or ""
    chat_username = chat.username or ""
    haystack = f"{chat_title} {chat_username}"

    # 1. Title / username keyword filter
    match = NOISE_TITLE_REGEX.search(haystack)
    if match:
        print(
            f"🚫 Skipping noise chat '{chat_title}' "
            f"(matched pattern: '{match.group(0)}')"
        )
        return False

    # 2. Broadcast-only channels: skip channels where messages have no
    #    from_user (pure broadcast) — they add little conversational value.
    if str(chat.type) == "ChatType.CHANNEL":
        print(f"🚫 Skipping broadcast-only channel '{chat_title}'")
        return False

    # 3. Sample recent history to compute text-to-media ratio
    try:
        total = 0
        text_bearing = 0
        async for message in userbot.get_chat_history(
            chat.id, limit=FILTER_SAMPLE_SIZE
        ):
            total += 1
            raw_text = message.text or message.caption or ""
            if len(raw_text.strip()) >= 10:
                text_bearing += 1

        if total >= FILTER_MIN_SAMPLES:
            ratio = text_bearing / total
            if ratio < MIN_TEXT_RATIO:
                print(
                    f"🚫 Skipping low-text chat '{chat_title}' "
                    f"(text ratio {ratio:.2f} < {MIN_TEXT_RATIO})"
                )
                return False
    except errors.FloodWait as e:
        print(f"⚠️ FloodWait while filtering '{chat_title}', sleeping {e.value}s...")
        await asyncio.sleep(e.value)
    except Exception as e:
        print(f"⚠️ Filter sample error for '{chat_title}': {e}")

    return True


# ==========================================
# 0. STORAGE SHIELD (capacity guard)
# ==========================================
async def StorageShield():
    """Check the messages collection size against MAX_VECTORS.

    - At >= 80% capacity: send a one-time Telegram DM warning.
    - At >= 95% capacity: set `scraping_paused = True` so background scrapers stop.
    - Below 80%: reset the alert flag so future warnings can fire again.
    """
    global alert_sent, scraping_paused
    try:
        collection_info = await asyncio.to_thread(
            qdrant.get_collection, MESSAGES_COLLECTION
        )
        # Prefer vectors_count; fall back to points_count if unavailable.
        vectors_count = (
            getattr(collection_info, "vectors_count", None)
            or getattr(collection_info, "points_count", None)
            or 0
        )

        usage_ratio = vectors_count / MAX_VECTORS if MAX_VECTORS else 0.0
        print(
            f"🛡️ StorageShield: {vectors_count:,}/{MAX_VECTORS:,} vectors "
            f"({usage_ratio * 100:.1f}% capacity)."
        )

        # --- Pause threshold (95%) ---
        if usage_ratio >= STORAGE_PAUSE_RATIO:
            if not scraping_paused:
                scraping_paused = True
                print("⛔ StorageShield: capacity >= 95%, pausing background scraping.")
                if ADMIN_USER_ID:
                    try:
                        await inline_bot.send_message(
                            chat_id=ADMIN_USER_ID,
                            text=(
                                f"⛔ **SCRAPING PAUSED — STORAGE CRITICAL**\n\n"
                                f"Vectors: `{vectors_count:,}` / `{MAX_VECTORS:,}` "
                                f"({usage_ratio * 100:.1f}%)\n"
                                f"Background ingestion has been paused to protect the cluster.\n"
                                f"Please purge old messages or upgrade your Qdrant plan."
                            ),
                        )
                    except errors.PeerIdInvalid:
                        print("⚠️ Cannot DM admin (PeerIdInvalid). Click /start on the bot.")
                    except Exception as e:
                        print(f"⚠️ Failed to send pause alert: {e}")

        # --- Warning threshold (80%) ---
        elif usage_ratio >= STORAGE_WARN_RATIO:
            if not alert_sent:
                alert_sent = True
                print("⚠️ StorageShield: capacity >= 80%, sending warning DM.")
                if ADMIN_USER_ID:
                    try:
                        await inline_bot.send_message(
                            chat_id=ADMIN_USER_ID,
                            text=(
                                f"⚠️ **QDRANT STORAGE WARNING** ⚠️\n\n"
                                f"Your database is approaching its capacity limit!\n"
                                f"Vectors: `{vectors_count:,}` / `{MAX_VECTORS:,}` "
                                f"({usage_ratio * 100:.1f}%)\n"
                                f"Scraping will pause automatically at "
                                f"{int(STORAGE_PAUSE_RATIO * 100)}%."
                            ),
                        )
                    except errors.PeerIdInvalid:
                        print("⚠️ Cannot DM admin (PeerIdInvalid). Click /start on the bot.")
                    except Exception as e:
                        print(f"⚠️ Failed to send warning alert: {e}")

        # --- Healthy range: reset flags so alerts can fire again later ---
        else:
            if alert_sent:
                alert_sent = False
            if scraping_paused:
                scraping_paused = False
                print("✅ StorageShield: capacity back under 95%, resuming scraping.")
    except Exception as e:
        print(f"StorageShield error: {e}")

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
        message_link = build_message_link(chat, message.id)

        # 4. Upsert Chat Metadata (Vectorized so CHATS_COLLECTION is semantically searchable)
        chat_point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"chat_{chat.id}"))
        chat_embed_text = f"Group: {chat_title}" + (f" (@{chat_username})" if chat_username else "")
        chat_embedding = (await embed_texts([chat_embed_text]))[0].tolist()

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

        # 5. Embed message and store full payload (offloaded to a thread)
        msg_embedding = (await embed_texts([text]))[0].tolist()
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
        # 1. Embed user query (offloaded to a thread)
        query_vector = (await embed_texts([query_text]))[0].tolist()

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

        # 6. Synthesize with DeepSeek (offloaded to a thread)
        ai_response = await asyncio.to_thread(
            deepseek_client.chat.completions.create,
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

                # Skip noise channels (NSFW, memes, gambling, sticker-only, etc.)
                if not await is_high_value_chat(chat):
                    continue

                # 1. Upsert Chat Metadata into CHATS_COLLECTION (vectorized, offloaded to a thread)
                chat_point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"chat_{chat.id}"))
                chat_embed_text = f"Group: {chat_title}" + (f" (@{chat_username})" if chat_username else "")
                chat_embedding = (await embed_texts([chat_embed_text]))[0].tolist()

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
                embeddings = await embed_texts(texts)

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
                    message_link = build_message_link(chat, message.id)

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
# 4. CAPACITY MONITOR (delegates to StorageShield)
# ==========================================
async def monitor_qdrant_capacity():
    while True:
        await StorageShield()
        await asyncio.sleep(STORAGE_CHECK_INTERVAL_SECONDS)


# ==========================================
# 5. CONTINUOUS DEEP BACKGROUND SCRAPER
# ==========================================
async def continuous_deep_scraper():
    """Continuously walk every dialog and paginate deep history.

    Runs forever, sleeping between full passes. Respects `scraping_paused`
    set by StorageShield and backs off on FloodWait.
    """
    print("🕳️ Continuous deep scraper started.")
    while True:
        if scraping_paused:
            print("⏸️ Deep scraper idle: storage paused.")
            await asyncio.sleep(SCRAPER_IDLE_SLEEP_SECONDS)
            continue

        try:
            async for dialog in userbot.get_dialogs():
                if scraping_paused:
                    print("⏸️ Deep scraper stopping pass: storage paused.")
                    break

                chat = dialog.chat
                chat_title = chat.title or chat.first_name or "Private Chat"
                chat_username = chat.username or ""

                # Skip noise channels (NSFW, memes, gambling, sticker-only, etc.)
                if not await is_high_value_chat(chat):
                    continue

                # Upsert chat metadata (vectorized)
                try:
                    chat_point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"chat_{chat.id}"))
                    chat_embed_text = (
                        f"Group: {chat_title}"
                        + (f" (@{chat_username})" if chat_username else "")
                    )
                    chat_embedding = (await embed_texts([chat_embed_text]))[0].tolist()
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
                                },
                            )
                        ],
                    )
                except Exception as e:
                    print(f"Deep scraper: chat metadata error for {chat.id}: {e}")

                # Paginate deep history
                offset_id = 0
                while True:
                    if scraping_paused:
                        break
                    try:
                        batch = []
                        async for message in userbot.get_chat_history(
                            chat.id, limit=SCRAPER_PAGE_SIZE, offset_id=offset_id
                        ):
                            raw_text = message.text or message.caption or ""
                            clean_text = raw_text.strip()
                            if len(clean_text) >= 10:
                                batch.append((message, clean_text))
                            offset_id = message.id

                        if not batch:
                            break

                        texts = [item[1] for item in batch]
                        embeddings = await embed_texts(texts)

                        points = []
                        for (message, text), embedding in zip(batch, embeddings):
                            if message.from_user:
                                sender_id = message.from_user.id
                                first_name = message.from_user.first_name or ""
                                last_name = message.from_user.last_name or ""
                                sender_name = (
                                    f"{first_name} {last_name}".strip() or "Unknown"
                                )
                                sender_username = message.from_user.username or ""
                            else:
                                sender_id = chat.id
                                sender_name = chat_title
                                sender_username = chat_username

                            message_link = build_message_link(chat, message.id)
                            point_id = str(
                                uuid.uuid5(
                                    uuid.NAMESPACE_DNS, f"{chat.id}_{message.id}"
                                )
                            )
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
                                        "link": message_link,
                                    },
                                )
                            )

                        qdrant.upsert(
                            collection_name=MESSAGES_COLLECTION, points=points
                        )
                        await asyncio.sleep(0.2)

                        if len(batch) < SCRAPER_PAGE_SIZE:
                            break
                    except errors.FloodWait as e:
                        print(
                            f"⚠️ Deep scraper FloodWait on chat {chat.id}, "
                            f"sleeping {e.value}s..."
                        )
                        await asyncio.sleep(e.value)
                    except Exception as e:
                        print(f"Deep scraper: history error for chat {chat.id}: {e}")
                        break

        except Exception as e:
            print(f"Deep scraper pass error: {e}")

        print("✅ Deep scraper pass complete. Sleeping before next pass...")
        await asyncio.sleep(SCRAPER_IDLE_SLEEP_SECONDS)


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
    asyncio.create_task(continuous_deep_scraper())
    asyncio.create_task(monitor_qdrant_capacity())

    print("🟢 All services online. Listening continuously...")
    await asyncio.Event().wait()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nShutting down engine...")
