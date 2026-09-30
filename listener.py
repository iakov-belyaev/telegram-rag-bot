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

        # Update chat info in 'telegram_chats'
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

        # Vectorize message
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

        # Upsert point into Qdrant
        qdrant.upsert(
            collection_name=MESSAGES_COLLECTION,
            points=[
                PointStruct(
                    id=point_id,
                    vector=embedding.tolist(),
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