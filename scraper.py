import asyncio
import os
import uuid
from dotenv import load_dotenv
from fastembed import TextEmbedding
from pyrogram import Client, errors
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

load_dotenv()

# 1. Initialize FastEmbed model (384 dimensions)
print("Loading FastEmbed model...")
embedding_model = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")

# 2. Connect to Qdrant Cloud
qdrant = QdrantClient(
    url=os.getenv("QDRANT_URL"),
    api_key=os.getenv("QDRANT_API_KEY")
)

COLLECTION_NAME = "telegram_messages"

# 3. Initialize Pyrogram Userbot
app = Client(
    "my_userbot",
    api_id=int(os.getenv("TELEGRAM_API_ID")),
    api_hash=os.getenv("TELEGRAM_API_HASH")
)

async def scrape_and_vectorize():
    async with app:
        print("Logged in as userbot. Fetching dialogs...")
        
        async for dialog in app.get_dialogs():
            chat = dialog.chat
            chat_title = chat.title or chat.first_name or "Private Chat"
            print(f"\n--- Scraping Chat: {chat_title} (ID: {chat.id}) ---")

            try:
                # Adjust limit per chat as needed (e.g. limit=500)
                async for message in app.get_chat_history(chat.id, limit=200):
                    if not message.text or len(message.text.strip()) < 10:
                        continue  # Skip empty or short messages (e.g. "ok", "lol")

                    text = message.text.strip()

                    # Generate embedding vector
                    embedding = list(embedding_model.embed([text]))[0]

                    # Extract metadata
                    sender_id = message.from_user.id if message.from_user else chat.id
                    sender_name = message.from_user.first_name if message.from_user else "Unknown"
                    message_link = message.link if hasattr(message, "link") and message.link else f"https://t.me/c/{abs(chat.id)}/{message.id}"

                    # Unique deterministic UUID for Qdrant
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

                    # Upsert vector into Qdrant
                    qdrant.upsert(
                        collection_name=COLLECTION_NAME,
                        points=[
                            PointStruct(
                                id=point_id,
                                vector=embedding.tolist(),
                                payload=payload
                            )
                        ]
                    )
                    print(f"Indexed message from {sender_name} in '{chat_title}'")

            except errors.FloodWait as e:
                print(f"Telegram rate limit hit. Waiting for {e.value} seconds...")
                await asyncio.sleep(e.value)
            except Exception as e:
                print(f"Error scraping chat {chat_title}: {e}")

if __name__ == "__main__":
    app.run(scrape_and_vectorize())