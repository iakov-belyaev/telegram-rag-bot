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

MESSAGES_COLLECTION = "telegram_messages"
CHATS_COLLECTION = "telegram_chats"

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
                # --- Step 1: Register/Update Chat in 'telegram_chats' ---
                chat_point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"chat_{chat.id}"))
                qdrant.upsert(
                    collection_name=CHATS_COLLECTION,
                    points=[
                        PointStruct(
                            id=chat_point_id,
                            vector=[0.0],  # Dummy vector for non-vector metadata storage
                            payload={
                                "chat_id": chat.id,
                                "chat_title": chat_title,
                                "chat_type": str(chat.type),
                                "username": chat.username or "",
                                "members_count": getattr(chat, "members_count", 0),
                            }
                        )
                    ]
                )
                print(f"Registered chat metadata in DB: '{chat_title}'")

                # --- Step 2: Fetch and Filter Messages ---
                messages_to_process = []
                async for message in app.get_chat_history(chat.id, limit=200):
                    if message.text and len(message.text.strip()) >= 10:
                        messages_to_process.append(message)

                if not messages_to_process:
                    print(f"No valid messages found in '{chat_title}'. Skipping vectorization.")
                    continue

                # --- Step 3: Batch Vectorize Messages ---
                texts = [m.text.strip() for m in messages_to_process]
                embeddings = list(embedding_model.embed(texts))

                # --- Step 4: Construct Points for Batch Upsert ---
                points = []
                for message, embedding in zip(messages_to_process, embeddings):
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
                        "text": message.text.strip(),
                        "date": str(message.date),
                        "link": message_link
                    }

                    points.append(
                        PointStruct(
                            id=point_id,
                            vector=embedding.tolist(),
                            payload=payload
                        )
                    )

                # --- Step 5: Upsert Batch into 'telegram_messages' ---
                qdrant.upsert(
                    collection_name=MESSAGES_COLLECTION,
                    points=points
                )
                print(f"Successfully vector-indexed {len(points)} messages from '{chat_title}'")

            except errors.FloodWait as e:
                print(f"Telegram rate limit hit. Waiting for {e.value} seconds...")
                await asyncio.sleep(e.value)
            except Exception as e:
                print(f"Error scraping chat '{chat_title}': {e}")

if __name__ == "__main__":
    app.run(scrape_and_vectorize())