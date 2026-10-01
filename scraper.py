import asyncio
import os
from dotenv import load_dotenv
from pyrogram import Client, errors
from qdrant_client.models import PointStruct

from core import (
    MESSAGES_COLLECTION,
    build_message_payload,
    embed_texts,
    message_point_id,
    qdrant,
    upsert_chat,
)

load_dotenv()

# Initialize Pyrogram Userbot
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
                # --- Step 1: Register/Update Chat in 'telegram_chats' (vectorized) ---
                await upsert_chat(chat)
                print(f"Registered chat metadata in DB: '{chat_title}'")

                # --- Step 2: Fetch and Filter Messages ---
                messages_to_process = []
                async for message in app.get_chat_history(chat.id, limit=450):
                    if message.text and len(message.text.strip()) >= 5:
                        messages_to_process.append(message)

                if not messages_to_process:
                    print(f"No valid messages found in '{chat_title}'. Skipping vectorization.")
                    continue

                # --- Step 3: Batch Vectorize Messages (offloaded to a thread) ---
                texts = [m.text.strip() for m in messages_to_process]
                embeddings = await embed_texts(texts)

                # --- Step 4: Construct Points for Batch Upsert ---
                points = []
                for message, embedding in zip(messages_to_process, embeddings):
                    payload = build_message_payload(
                        chat, message, message.text.strip(), source_type="text"
                    )
                    points.append(
                        PointStruct(
                            id=message_point_id(chat.id, message.id),
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
