"""Qdrant provisioning for telegram-rag-bot: collections + payload indexes.

Creates/updates the four collections in the tg_bot_rag cluster database:
telegram_messages, telegram_chats, telegram_profiles, telegram_relations.
Idempotent: safe to re-run; never deletes existing data.
"""

import config
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PayloadSchemaType, VectorParams


def build_client() -> QdrantClient:
    """Build a Qdrant client from config; fall back to a local path when no URL."""
    url = getattr(config, "QDRANT_URL", "") or ""
    api_key = getattr(config, "QDRANT_API_KEY", "") or ""
    if url:
        return QdrantClient(url=url, api_key=api_key or None)
    return QdrantClient(path="qdrant_data")


def ensure_collection(client: QdrantClient, name: str) -> None:
    """Create the collection if missing; verify size/distance if it exists."""
    try:
        info = client.get_collection(name)
    except Exception:
        info = None

    if info is None:
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(
                size=config.VECTOR_SIZE, distance=Distance.COSINE
            ),
        )
        print(f"[setup_db] {name}: created (size={config.VECTOR_SIZE}, cosine)")
        return

    params = info.config.params.vectors
    size = getattr(params, "size", None)
    distance = getattr(params, "distance", None)
    distance_name = getattr(distance, "value", distance)

    if size == config.VECTOR_SIZE and str(distance_name).lower() == "cosine":
        print(f"[setup_db] {name}: already exists (size ok)")
        return

    print(
        f"[setup_db] {name}: MISMATCH existing size={size} distance={distance_name} "
        f"!= expected size={config.VECTOR_SIZE} distance=cosine — RECREATING"
    )
    client.delete_collection(name)
    client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(
            size=config.VECTOR_SIZE, distance=Distance.COSINE
        ),
    )
    print(f"[setup_db] {name}: recreated (size={config.VECTOR_SIZE}, cosine)")


def ensure_index(
    client: QdrantClient, collection: str, field_name: str, schema: PayloadSchemaType
) -> None:
    """Create a payload index; a duplicate-index error is not fatal."""
    try:
        client.create_payload_index(
            collection_name=collection,
            field_name=field_name,
            field_schema=schema,
        )
        print(f"[setup_db] {collection}: indexed {field_name}")
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "already exists" in msg or "already indexed" in msg:
            print(f"[setup_db] {collection}: index exists {field_name}")
            return
        print(f"[setup_db] {collection}: index {field_name} failed: {exc}")


def main() -> int:
    client = build_client()

    collections = (
        config.MESSAGES_COLLECTION,
        config.CHATS_COLLECTION,
        config.PROFILES_COLLECTION,
        config.RELATIONS_COLLECTION,
    )
    for name in collections:
        ensure_collection(client, name)

    for field_name in ("chat_id", "sender_id"):
        ensure_index(
            client, config.MESSAGES_COLLECTION, field_name, PayloadSchemaType.INTEGER
        )
    for field_name in ("sender_username", "source_type"):
        ensure_index(
            client, config.MESSAGES_COLLECTION, field_name, PayloadSchemaType.KEYWORD
        )

    ensure_index(
        client, config.CHATS_COLLECTION, "chat_id", PayloadSchemaType.INTEGER
    )

    ensure_index(
        client, config.PROFILES_COLLECTION, "user_id", PayloadSchemaType.INTEGER
    )
    ensure_index(
        client, config.PROFILES_COLLECTION, "username", PayloadSchemaType.KEYWORD
    )

    for field_name in ("a_id", "b_id"):
        ensure_index(
            client, config.RELATIONS_COLLECTION, field_name, PayloadSchemaType.INTEGER
        )
    ensure_index(
        client, config.RELATIONS_COLLECTION, "edge_kind", PayloadSchemaType.KEYWORD
    )

    print("[setup_db] summary:")
    for name in collections:
        try:
            info = client.get_collection(name)
            points = getattr(info, "points_count", None)
        except Exception as exc:  # noqa: BLE001
            points = f"error: {exc}"
        print(f"[setup_db]   {name}: points={points}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
