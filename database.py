"""Data layer for telegram-rag-bot: Qdrant collections, embeddings, payload building.

Supersedes core.py. Import-safe: clients and the embedding model are lazy.
"""

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from fastembed import TextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
)
from qdrant_client.http.exceptions import UnexpectedResponse
from tenacity import retry, stop_after_attempt, wait_exponential

import config

# --- Collection names (single source of truth: config.py) ---
MESSAGES_COLLECTION = config.MESSAGES_COLLECTION
CHATS_COLLECTION = config.CHATS_COLLECTION
PROFILES_COLLECTION = config.PROFILES_COLLECTION
RELATIONS_COLLECTION = config.RELATIONS_COLLECTION

# --- Lazy singletons ---
_qdrant_client: Optional[QdrantClient] = None
_embedding_model: Optional[TextEmbedding] = None


def get_qdrant() -> QdrantClient:
    """Return the module-global Qdrant client, creating it on first use."""
    global _qdrant_client
    if _qdrant_client is None:
        _qdrant_client = QdrantClient(
            url=config.QDRANT_URL,
            api_key=config.QDRANT_API_KEY,
            timeout=config.QDRANT_TIMEOUT_SECONDS,
        )
    return _qdrant_client


def get_embedding_model() -> TextEmbedding:
    """Return the module-global FastEmbed model, loading it on first use."""
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = TextEmbedding(model_name=config.EMBEDDING_MODEL)
    return _embedding_model


# --- Retry / timeout helper ---
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=15),
    reraise=True,
)
async def _qdrant_call(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Run a sync Qdrant callable in a worker thread with timeout + retry."""
    return await asyncio.wait_for(
        asyncio.to_thread(fn, *args, **kwargs),
        timeout=config.QDRANT_TIMEOUT_SECONDS,
    )


def _is_missing_collection_error(exc: Exception) -> bool:
    """True when a Qdrant error means the target collection does not exist yet."""
    if isinstance(exc, UnexpectedResponse):
        text = str(exc).lower()
        return "doesn't exist" in text or "not found" in text or "404" in text
    if isinstance(exc, ValueError):
        return "not found" in str(exc).lower()
    return False


async def _qdrant_read(default: Any, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Like _qdrant_call, but returns `default` when the target collection is missing."""
    try:
        return await _qdrant_call(fn, *args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        if _is_missing_collection_error(exc):
            return default
        raise


# --- Embeddings ---
async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts in batches, returning plain Python lists of floats."""
    items = list(texts)
    model = get_embedding_model()
    batch_size = config.EMBED_BATCH_SIZE
    vectors: list[list[float]] = []
    for start in range(0, len(items), batch_size):
        chunk = items[start : start + batch_size]
        chunk_vectors = await asyncio.to_thread(lambda c=chunk: list(model.embed(c)))
        vectors.extend(v.tolist() for v in chunk_vectors)
    return vectors


# --- Deterministic point IDs (LEGACY-COMPATIBLE, DO NOT CHANGE FORMULAS) ---
def message_point_id(chat_id: int, message_id: int) -> str:
    """Deterministic Qdrant point ID for a message."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{chat_id}_{message_id}"))


def chat_point_id(chat_id: int) -> str:
    """Deterministic Qdrant point ID for a chat."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"chat_{chat_id}"))


def profile_point_id(user_id: int) -> str:
    """Deterministic Qdrant point ID for a user profile."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"profile_{user_id}"))


def relation_point_id(user_a_id: int, user_b_id: int, kind: str) -> str:
    """Deterministic Qdrant point ID for a relation edge."""
    lo, hi = sorted((user_a_id, user_b_id))
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"relation_{lo}_{hi}_{kind}"))


# --- Pure payload builders (sync, no network) ---
def build_message_link(chat: Any, message_id: int) -> str:
    """Build a Telegram deep-link for private chats, groups, and channels."""
    if getattr(chat, "username", None):
        return f"https://t.me/{chat.username}/{message_id}"
    clean_chat_id = str(chat.id).replace("-100", "").replace("-", "")
    return f"https://t.me/c/{clean_chat_id}/{message_id}"


def build_message_payload(
    chat: Any, message: Any, text: str, source_type: str = "text"
) -> dict:
    """Build the Qdrant payload dict for a Telegram message (legacy-compatible)."""
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

    reply = getattr(message, "reply_to_message", None)
    reply_to_user_id = getattr(getattr(reply, "from_user", None), "id", None) if reply else None
    reply_to_message_id = getattr(reply, "id", None) if reply else None

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
        "reply_to_user_id": reply_to_user_id,
        "reply_to_message_id": reply_to_message_id,
    }


def build_profile_payload(
    user_id: int,
    username: str = "",
    first_name: str = "",
    last_name: str = "",
    bio: str = "",
    pfp_hash: str = "",
    pfp_meta: Optional[dict] = None,
    common_chat_ids: Optional[list[int]] = None,
    common_chat_titles: Optional[list[str]] = None,
    message_count: int = 0,
    interactions: Optional[dict] = None,
    first_seen: str = "",
    last_seen: str = "",
    is_bot: bool = False,
    is_premium: Optional[bool] = None,
) -> dict:
    """Build the Qdrant payload dict for a user profile."""
    return {
        "user_id": user_id,
        "username": username,
        "display_name": f"{first_name} {last_name}".strip(),
        "bio": bio,
        "pfp_hash": pfp_hash,
        "pfp_meta": pfp_meta if pfp_meta is not None else {},
        "common_chat_ids": sorted(common_chat_ids) if common_chat_ids else [],
        "common_chat_titles": list(common_chat_titles) if common_chat_titles else [],
        "message_count": message_count,
        "interactions": dict(interactions) if interactions else {},
        "first_seen": first_seen,
        "last_seen": last_seen,
        "is_bot": is_bot,
        "is_premium": is_premium,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "kind": "profile",
    }


def build_profile_embed_text(profile: dict) -> str:
    """Build a deterministic embedding sentence for a profile payload."""
    username = profile.get("username") or ""
    display_name = profile.get("display_name") or ""
    bio = profile.get("bio") or ""
    chat_titles = profile.get("common_chat_titles") or []
    message_count = profile.get("message_count") or 0

    header = f"User @{username} ({display_name})." if username else f"User ({display_name})."
    segments = [header]
    if bio:
        segments.append(f"Bio: {bio}.")
    if chat_titles:
        titles = ", ".join(chat_titles[:10])
        segments.append(f"Active in {len(chat_titles)} groups: {titles}.")
    if message_count:
        segments.append(f"Wrote {message_count} indexed messages.")
    return " ".join(segments)


def build_relation_payload(
    a_id: int,
    a_username: str,
    b_id: int,
    b_username: str,
    kind: str,
    weight: float,
    evidence_chat_ids: list[int],
    first_seen: str,
    last_seen: str,
) -> dict:
    """Build the Qdrant payload dict for a relation edge."""
    if a_id <= b_id:
        lo_id, hi_id = a_id, b_id
        lo_username, hi_username = a_username, b_username
    else:
        lo_id, hi_id = b_id, a_id
        lo_username, hi_username = b_username, a_username

    return {
        "a_id": lo_id,
        "b_id": hi_id,
        "a_username": lo_username,
        "b_username": hi_username,
        "edge_kind": kind,
        "weight": weight,
        "evidence_chat_ids": list(evidence_chat_ids) if evidence_chat_ids else [],
        "first_seen": first_seen,
        "last_seen": last_seen,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "kind": "relation",
    }


def build_relation_embed_text(rel: dict) -> str:
    """Build a deterministic embedding sentence for a relation payload."""
    a_username = rel.get("a_username") or ""
    b_username = rel.get("b_username") or ""
    edge_kind = rel.get("edge_kind") or ""
    weight = rel.get("weight") or 0
    chat_ids = rel.get("evidence_chat_ids") or []
    return (
        f"Relation {edge_kind} between @{a_username} and @{b_username}. "
        f"Weight {weight}. Shared chats: {len(chat_ids)}."
    )


# --- Async upserts ---
async def upsert_chat(chat: Any) -> None:
    """Vectorize and upsert chat metadata into CHATS_COLLECTION."""
    chat_title = chat.title or chat.first_name or "Private Chat"
    chat_username = chat.username or ""
    chat_embed_text = f"Group: {chat_title}" + (
        f" (@{chat_username})" if chat_username else ""
    )
    chat_embedding = (await embed_texts([chat_embed_text]))[0]

    point = PointStruct(
        id=chat_point_id(chat.id),
        vector=chat_embedding,
        payload={
            "chat_id": chat.id,
            "chat_title": chat_title,
            "chat_type": str(chat.type),
            "username": chat_username,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    await _qdrant_call(
        get_qdrant().upsert,
        collection_name=CHATS_COLLECTION,
        points=[point],
    )


async def upsert_messages(points: list[PointStruct]) -> int:
    """Upsert message points in batches; return the number of points sent."""
    batch_size = config.QDRANT_UPSERT_BATCH_SIZE
    total = 0
    for start in range(0, len(points), batch_size):
        chunk = points[start : start + batch_size]
        await _qdrant_call(
            get_qdrant().upsert,
            collection_name=MESSAGES_COLLECTION,
            points=chunk,
        )
        total += len(chunk)
    return total


async def upsert_profiles(items: list[tuple[int, dict, list[float]]]) -> int:
    """Upsert profile points; return the number of points sent."""
    batch_size = config.QDRANT_UPSERT_BATCH_SIZE
    total = 0
    for start in range(0, len(items), batch_size):
        chunk = items[start : start + batch_size]
        points = [
            PointStruct(id=profile_point_id(user_id), vector=vector, payload=payload)
            for user_id, payload, vector in chunk
        ]
        await _qdrant_call(
            get_qdrant().upsert,
            collection_name=PROFILES_COLLECTION,
            points=points,
        )
        total += len(points)
    return total


async def upsert_relations(items: list[tuple[dict, list[float]]]) -> int:
    """Upsert relation points; return the number of points sent."""
    batch_size = config.QDRANT_UPSERT_BATCH_SIZE
    total = 0
    for start in range(0, len(items), batch_size):
        chunk = items[start : start + batch_size]
        points = [
            PointStruct(
                id=relation_point_id(
                    payload["a_id"], payload["b_id"], payload["edge_kind"]
                ),
                vector=vector,
                payload=payload,
            )
            for payload, vector in chunk
        ]
        await _qdrant_call(
            get_qdrant().upsert,
            collection_name=RELATIONS_COLLECTION,
            points=points,
        )
        total += len(points)
    return total


# --- Async reads ---
async def search_messages(
    query: str,
    limit: int = 10,
    chat_id: Optional[int] = None,
    sender_username: Optional[str] = None,
    score_threshold: Optional[float] = None,
) -> list:
    """Semantic search over indexed messages with optional filters."""
    vector = (await embed_texts([query]))[0]

    must: list[FieldCondition] = []
    if chat_id is not None:
        must.append(FieldCondition(key="chat_id", match=MatchValue(value=chat_id)))
    if sender_username is not None:
        must.append(
            FieldCondition(key="sender_username", match=MatchValue(value=sender_username))
        )
    query_filter = Filter(must=must) if must else None

    response = await _qdrant_read(
        None,
        get_qdrant().query_points,
        collection_name=MESSAGES_COLLECTION,
        query=vector,
        limit=limit,
        query_filter=query_filter,
        score_threshold=score_threshold,
    )
    if response is None:
        return []
    return response.points


async def search_profiles(query: str, limit: int = 10) -> list:
    """Semantic search over indexed profiles."""
    vector = (await embed_texts([query]))[0]
    response = await _qdrant_read(
        None,
        get_qdrant().query_points,
        collection_name=PROFILES_COLLECTION,
        query=vector,
        limit=limit,
    )
    if response is None:
        return []
    return response.points


async def get_profile(user_id: int) -> Optional[dict]:
    """Retrieve a single profile payload by user id, or None if absent."""
    records = await _qdrant_read(
        [],
        get_qdrant().retrieve,
        collection_name=PROFILES_COLLECTION,
        ids=[profile_point_id(user_id)],
        with_vectors=False,
    )
    if not records:
        return None
    return records[0].payload


async def get_profiles_bulk(user_ids: list[int]) -> dict[int, dict]:
    """Retrieve multiple profile payloads keyed by user id."""
    result: dict[int, dict] = {}
    ids = [profile_point_id(uid) for uid in user_ids]
    for start in range(0, len(ids), 256):
        chunk = ids[start : start + 256]
        records = await _qdrant_read(
            [],
            get_qdrant().retrieve,
            collection_name=PROFILES_COLLECTION,
            ids=chunk,
            with_vectors=False,
        )
        for record in records:
            payload = record.payload or {}
            if "user_id" in payload:
                result[payload["user_id"]] = payload
    return result


async def get_relations_for(user_id: int, limit: int = 50) -> list[dict]:
    """Return relation payloads involving a user, sorted by weight desc."""
    scroll_filter = Filter(
        should=[
            FieldCondition(key="a_id", match=MatchValue(value=user_id)),
            FieldCondition(key="b_id", match=MatchValue(value=user_id)),
        ]
    )
    records, _ = await _qdrant_read(
        ([], None),
        get_qdrant().scroll,
        collection_name=RELATIONS_COLLECTION,
        scroll_filter=scroll_filter,
        limit=limit,
        with_vectors=False,
    )
    payloads = [record.payload for record in records if record.payload]
    payloads.sort(key=lambda p: p.get("weight", 0), reverse=True)
    return payloads[:limit]


async def filter_existing_message_ids(
    chat_id: int, message_ids: list[int]
) -> set[int]:
    """Return the subset of message_ids that already exist in the cluster."""
    if not message_ids:
        return set()

    id_to_message: dict[str, int] = {
        message_point_id(chat_id, mid): mid for mid in message_ids
    }
    point_ids = list(id_to_message.keys())
    existing: set[int] = set()
    for start in range(0, len(point_ids), 256):
        chunk = point_ids[start : start + 256]
        records = await _qdrant_read(
            [],
            get_qdrant().retrieve,
            collection_name=MESSAGES_COLLECTION,
            ids=chunk,
            with_vectors=False,
        )
        for record in records:
            mid = id_to_message.get(str(record.id))
            if mid is not None:
                existing.add(mid)
    return existing


async def collection_stats() -> dict:
    """Return point counts and existence flags for all collections."""
    collections = await _qdrant_call(get_qdrant().get_collections)
    existing_names = {c.name for c in collections.collections}

    stats: dict[str, Any] = {}
    total_points = 0
    for name in (
        MESSAGES_COLLECTION,
        CHATS_COLLECTION,
        PROFILES_COLLECTION,
        RELATIONS_COLLECTION,
    ):
        if name not in existing_names:
            stats[name] = {"points": None, "exists": False}
            continue
        info = await _qdrant_call(get_qdrant().get_collection, name)
        points = info.points_count
        stats[name] = {"points": points, "exists": True}
        if points:
            total_points += points

    messages_points = stats[MESSAGES_COLLECTION]["points"] or 0
    max_vectors = config.MAX_VECTORS
    stats["messages_usage"] = {
        "max_vectors": max_vectors,
        "usage_ratio": (messages_points / max_vectors) if max_vectors else 0.0,
    }
    stats["total_points"] = total_points
    return stats
