"""Shared core module: initialized clients and reusable helpers.

Centralizes the Qdrant client, DeepSeek (OpenAI-compatible) async client,
FastEmbed model, and common helpers used across the scraper, listener,
inline bot, and setup scripts.
"""

import asyncio
import os
import uuid

from dotenv import load_dotenv
from fastembed import TextEmbedding
from openai import AsyncOpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

load_dotenv()

# --- Collection names ---
MESSAGES_COLLECTION = "telegram_messages"
CHATS_COLLECTION = "telegram_chats"

# --- Shared clients / models ---
print("Loading FastEmbed model (shared core)...")
embedding_model = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")

qdrant = QdrantClient(
    url=os.getenv("QDRANT_URL"),
    api_key=os.getenv("QDRANT_API_KEY"),
)

deepseek_client = AsyncOpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
)


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


def build_message_payload(chat, message, text, source_type="text"):
    """Build the Qdrant payload dict for a Telegram message.

    Handles sender extraction (user vs. channel), deep-link generation, and
    normalizes the payload shape used across all ingestion paths.
    """
    chat_title = chat.title or chat.first_name or "Private Chat"
    chat_username = chat.username or ""

    if getattr(message, "from_user", None):
        sender_id = message.from_user.id
        first_name = message.from_user.first_name or ""
        last_name = message.from_user.last_name or ""
        sender_name = f"{first_name} {last_name}".strip() or "Unknown"
        sender_username = message.from_user.username or ""
    else:
        sender_id = chat.id
        sender_name = chat_title
        sender_username = chat_username

    return {
        "message_id": message.id,
        "chat_id": chat.id,
        "chat_title": chat_title,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "sender_username": sender_username,
        "text": text,
        "source_type": source_type,
        "date": str(message.date),
        "link": build_message_link(chat, message.id),
    }


def message_point_id(chat_id, message_id):
    """Deterministic Qdrant point ID for a message."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{chat_id}_{message_id}"))


def chat_point_id(chat_id):
    """Deterministic Qdrant point ID for a chat."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"chat_{chat_id}"))


async def upsert_chat(chat):
    """Vectorize and upsert chat metadata into CHATS_COLLECTION."""
    chat_title = chat.title or chat.first_name or "Private Chat"
    chat_username = chat.username or ""
    chat_embed_text = f"Group: {chat_title}" + (
        f" (@{chat_username})" if chat_username else ""
    )
    chat_embedding = (await embed_texts([chat_embed_text]))[0].tolist()

    qdrant.upsert(
        collection_name=CHATS_COLLECTION,
        points=[
            PointStruct(
                id=chat_point_id(chat.id),
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
