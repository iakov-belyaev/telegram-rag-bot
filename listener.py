import os
from dotenv import load_dotenv
from pyrogram import Client, filters
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

@app.on_message(filters.text)
async def process_incoming_message(client, message):
    try:
        # Skip empty or short messages (under 10 characters)
        if not message.text or len(message.text.strip()) < 10:
            return

        chat = message.chat
        chat_title = chat.title or chat.first_name or "Private Chat"
        text = message.text.strip()

        # Update chat info in 'telegram_chats' (vectorized so it is semantically searchable)
        await upsert_chat(chat)

        # Vectorize message (offloaded to a thread)
        embedding = (await embed_texts([text]))[0].tolist()

        payload = build_message_payload(chat, message, text, source_type="text")

        # Upsert point into Qdrant
        qdrant.upsert(
            collection_name=MESSAGES_COLLECTION,
            points=[
                PointStruct(
                    id=message_point_id(chat.id, message.id),
                    vector=embedding,
                    payload=payload
                )
            ]
        )
        print(f"⚡ Live indexed message from {payload['sender_name']} in '{chat_title}'")

    except Exception as e:
        print(f"Error processing live message: {e}")

if __name__ == "__main__":
    print("🚀 Starting Real-Time Telegram Message Listener...")
    app.run()
