import os
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PayloadSchemaType

load_dotenv()

client = QdrantClient(
    url=os.getenv("QDRANT_URL"),
    api_key=os.getenv("QDRANT_API_KEY")
)

# 1. Collection for vector search on messages
if not client.collection_exists("telegram_messages"):
    client.create_collection(
        collection_name="telegram_messages",
        vectors_config=VectorParams(size=384, distance=Distance.COSINE),
    )
    # Index chat_id for fast filtering during vector searches
    client.create_payload_index(
        collection_name="telegram_messages",
        field_name="chat_id",
        field_schema=PayloadSchemaType.KEYWORD,
    )
    print("Collection 'telegram_messages' created.")

# 2. Collection for frontend group lookup
if not client.collection_exists("telegram_chats"):
    client.create_collection(
        collection_name="telegram_chats",
        # Small dummy vector required by Qdrant (size 1) for non-vector metadata storage
        vectors_config=VectorParams(size=1, distance=Distance.COSINE),
    )
    print("Collection 'telegram_chats' created.")