import os
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PayloadSchemaType

load_dotenv()

# Collection names
MESSAGES_COLLECTION = "telegram_messages"
CHATS_COLLECTION = "telegram_chats"

# 384 dimensions to match the FastEmbed all-MiniLM-L6-v2 model
VECTOR_SIZE = 384
VECTOR_DISTANCE = Distance.COSINE


def build_client():
    """Initialize Qdrant client from either an HTTP endpoint or a local disk path."""
    qdrant_url = os.getenv("QDRANT_URL")
    qdrant_api_key = os.getenv("QDRANT_API_KEY")
    qdrant_path = os.getenv("QDRANT_PATH")

    if qdrant_url:
        return QdrantClient(url=qdrant_url, api_key=qdrant_api_key)
    if qdrant_path:
        return QdrantClient(path=qdrant_path)
    # Fall back to the default local in-memory/disk behavior
    return QdrantClient(path="qdrant_data")


client = build_client()


def ensure_collection(name, vectors_config):
    """Create the collection with the desired config, recreating it if it already exists with the wrong config."""
    if client.collection_exists(name):
        info = client.get_collection(name)
        existing = info.config.params.vectors
        if (
            getattr(existing, "size", None) == vectors_config.size
            and getattr(existing, "distance", None) == vectors_config.distance
        ):
            print(f"Collection '{name}' already exists with the correct config.")
            return
        print(f"Collection '{name}' exists with the wrong config, recreating it.")
        client.delete_collection(name)

    client.create_collection(
        collection_name=name,
        vectors_config=vectors_config,
    )
    print(f"Collection '{name}' created.")


# 1. Collection for vector search on messages
ensure_collection(
    MESSAGES_COLLECTION,
    VectorParams(size=VECTOR_SIZE, distance=VECTOR_DISTANCE),
)
# Index chat_id for fast filtering during vector searches
client.create_payload_index(
    collection_name=MESSAGES_COLLECTION,
    field_name="chat_id",
    field_schema=PayloadSchemaType.KEYWORD,
)
# Index sender_username for per-user lookups
client.create_payload_index(
    collection_name=MESSAGES_COLLECTION,
    field_name="sender_username",
    field_schema=PayloadSchemaType.KEYWORD,
)
# Index source_type to filter by ingestion source (text, photo_ocr, document_*)
client.create_payload_index(
    collection_name=MESSAGES_COLLECTION,
    field_name="source_type",
    field_schema=PayloadSchemaType.KEYWORD,
)

# 2. Collection for frontend group lookup (vectorized so chats are semantically searchable)
ensure_collection(
    CHATS_COLLECTION,
    VectorParams(size=VECTOR_SIZE, distance=VECTOR_DISTANCE),
)
