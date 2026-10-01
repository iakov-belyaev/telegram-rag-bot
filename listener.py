import asyncio
import os
import uuid
from dotenv import load_dotenv
from fastembed import TextEmbedding
from pyrogram import Client, filters
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

load_dotenv()

# 1. Initialize FastEmbed model (384 dimensions)
print("Loading FastEmbed model for real-time listening...")
embedding_model = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")


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

# 2. Connect to Qdrant Cloud
qdrant = QdrantClient(
    url=os.getenv("QDRANT_URL"),
    api_key=os.getenv("QDRANT_API_KEY")
)

MESSAGES_COLLECTION = "telegram_messages"
CHATS_COLLECTION = "telegram_chats"

# 3. Initialize Pyrogram Userbot
app = Client(
    "my_userbot",
    api_id=int(os.getenv("TELEGRAM_API_ID")),
    api_hash=os.getenv("TELEGRAM_API_HASH")
)

@app.on_message(filters.text)
async def process_incoming_message(client, message):
    try:
        # Skip empty or short messages (under 10 characters)
        if not message.text or len(message.text.strip()) < 10:
            return

        chat = message.chat
        chat_title = chat.title or chat.first_name or "Private Chat"
        text = message.text.strip()

        chat_username = chat.username or ""

        # Update chat info in 'telegram_chats' (vectorized so it is semantically searchable)
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

        # Vectorize message (offloaded to a thread)
        embedding = (await embed_texts([text]))[0].tolist()

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

        message_link = build_message_link(chat, message.id)
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{chat.id}_{message.id}"))

        payload = {
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

        # Upsert point into Qdrant
        qdrant.upsert(
            collection_name=MESSAGES_COLLECTION,
            points=[
                PointStruct(
                    id=point_id,
                    vector=embedding,
                    payload=payload
                )
            ]
        )
        print(f"⚡ Live indexed message from {sender_name} in '{chat_title}'")

    except Exception as e:
        print(f"Error processing live message: {e}")

if __name__ == "__main__":
    print("🚀 Starting Real-Time Telegram Message Listener...")
    app.run()
