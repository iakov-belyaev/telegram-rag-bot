# Spec: `database.py` — Qdrant storage + embedding layer

Replace the stub in `database.py`. Python 3.11, full type hints, no prints at import.
This module supersedes the old `core.py` and is the single data-access layer for
all other modules (scraper, profiles, bot, api, setup).

## Hard rules

- IMPORT-SAFE: no network calls, no model loading, no printing at import time. The
  Qdrant client and FastEmbed model are LAZY singletons created on first use.
- Qdrant client calls are synchronous — every network call must run via
  `asyncio.to_thread` inside the shared retry/timeout helper described below.
  Public network API is async.
- Pure builders/IDs must be synchronous, side-effect free, and unit-testable with
  plain fake objects (attribute access only).
- Allowed imports: `asyncio`, `uuid`, `datetime`, `tenacity`, `qdrant_client`,
  `qdrant_client.models`, `fastembed`, `config`. Nothing else.

## Retry / timeout helper

```python
async def _qdrant_call(fn, /, *args, **kwargs):
    """Run a sync Qdrant callable in a worker thread with timeout + retry."""
```

- Use tenacity: up to 3 attempts, `wait_exponential(multiplier=1, min=1, max=15)`, re-raise on final failure.
- Per-attempt timeout: `config.QDRANT_TIMEOUT_SECONDS` via `asyncio.wait_for`.
- Return the callable's result.

## Lazy singletons

- `get_qdrant() -> QdrantClient` — module-global singleton:
  `QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=QDRANT_TIMEOUT_SECONDS)`.
- `get_embedding_model()` — module-global lazy `TextEmbedding(model_name=EMBEDDING_MODEL)`.
- `async def embed_texts(texts: list[str]) -> list[list[float]]` — converts input to `list`,
  splits into chunks of `config.EMBED_BATCH_SIZE`, runs `model.embed(chunk)` inside
  `asyncio.to_thread` per chunk, returns plain Python `list[list[float]]`.

## Deterministic point IDs (uuid5, DNS namespace) — LEGACY-COMPATIBLE, DO NOT CHANGE FORMULAS

- `message_point_id(chat_id: int, message_id: int) -> str` — `uuid5(NAMESPACE_DNS, f"{chat_id}_{message_id}")` (MUST match the 38k existing points in the live cluster).
- `chat_point_id(chat_id: int) -> str` — `f"chat_{chat_id}"`.
- `profile_point_id(user_id: int) -> str` — `f"profile_{user_id}"`.
- `relation_point_id(user_a_id: int, user_b_id: int, kind: str) -> str` — sorted ids, `f"relation_{lo}_{hi}_{kind}"`.

## Pure payload builders (sync, no network)

- `build_message_link(chat, message_id: int) -> str` — same logic as legacy:
  if `chat.username` → `https://t.me/{username}/{message_id}`; else strip `-100`/all `-` from
  `str(chat.id)` and return `https://t.me/c/{clean}/{message_id}`.

- `build_message_payload(chat, message, text: str, source_type: str = "text") -> dict` —
  EXACT legacy payload keys (compatibility with existing cluster data):
  `message_id, chat_id, chat_title, sender_id, sender_name, sender_username, text, source_type, date, link`.
  - `chat_title = chat.title or chat.first_name or "Private Chat"`.
  - If `getattr(message, "from_user", None)`: sender from that user (`first last`.strip() or "Unknown").
    Else: sender is the chat itself (sender_id/chat.id, sender_name/chat_title, sender_username/chat.username).
  - `date` = `str(message.date)`.

- `build_profile_payload(user_id, username="", first_name="", last_name="", bio="", pfp_hash="", pfp_meta=None, common_chat_ids=None, common_chat_titles=None, message_count=0, interactions=None, first_seen="", last_seen="", is_bot=False, is_premium=None) -> dict`
  - Returns keys: `user_id, username, display_name` ("first last".strip()), `bio, pfp_hash, pfp_meta,
    common_chat_ids (sorted list), common_chat_titles, message_count, interactions (dict),
    first_seen, last_seen, is_bot, is_premium, updated_at` (UTC ISO 8601 string), `kind="profile"`.
  - `None` inputs for `common_chat_ids`/`common_chat_titles`/`interactions` become `[]`/`[]`/`{}`.
  - Tags: include `tags: list[str]` param? NO — keep the exact signature above.

- `build_profile_embed_text(profile: dict) -> str` — deterministic sentence(s), robust to missing keys:
  `User @username (Display Name). Bio: <bio>. Active in N groups: <up to 10 chat titles>.`
  `Wrote M indexed messages.` Skip empty segments cleanly.

- `build_relation_payload(a_id, a_username, b_id, b_username, kind, weight, evidence_chat_ids, first_seen, last_seen) -> dict`
  - Sorted (lo, hi) ids for `a_id`/`b_id` AND their usernames swapped consistently.
  - Returns keys: `a_id, b_id, a_username, b_username, kind, weight, evidence_chat_ids (list), first_seen, last_seen, updated_at` (UTC ISO), `kind` field value is the edge type: one of `"co_member"`, `"replies"`, `"mentions"` — the payload key must be `rel_kind` to avoid double use of `kind`? Use payload key `edge_kind` for the type and keep `kind="relation"` marker field.

- `build_relation_embed_text(rel: dict) -> str` — deterministic sentence using display usernames + edge kind + weight + shared chat count.

## Async upserts (all via `_qdrant_call`; return number of points sent)

- `async def upsert_chat(chat) -> None` — legacy-compatible: embed `"Group: {title}"` (+ `(@username)`
  if present), point id `chat_point_id`, payload keys: `chat_id, chat_title, chat_type (str(chat.type)), username, updated_at`.
- `async def upsert_messages(points: list[PointStruct]) -> int` — chunk by `QDRANT_UPSERT_BATCH_SIZE`, one `_qdrant_call` per chunk.
- `async def upsert_profiles(items: list[tuple[int, dict, list[float]]]) -> int` — `(user_id, payload, vector)`; PointStruct id via `profile_point_id`.
- `async def upsert_relations(items: list[tuple[dict, list[float]]]) -> int` — `(payload, vector)`; id via `relation_point_id(payload["a_id"], payload["b_id"], payload["edge_kind"])`.

## Async reads

- `async def search_messages(query: str, limit: int = 10, chat_id: int | None = None, sender_username: str | None = None, score_threshold: float | None = None) -> list`
  — embed query via `embed_texts([query])[0]`; build `Filter(must=[...])` with `FieldCondition`/`MatchValue`
  for provided chat_id/sender_username; `query_points(collection_name=MESSAGES_COLLECTION, query=vector, limit=limit, query_filter=filter, score_threshold=score_threshold)`; return `response.points`.
- `async def search_profiles(query: str, limit: int = 10) -> list` — same pattern, PROFILES_COLLECTION, no filter.
- `async def get_profile(user_id: int) -> dict | None` — `retrieve(ids=[profile_point_id(user_id)], with_vectors=False)`; None if not found.
- `async def get_profiles_bulk(user_ids: list[int]) -> dict[int, dict]` — one retrieve call (chunk ids by 256); return `{user_id: payload}`.
- `async def get_relations_for(user_id: int, limit: int = 50) -> list[dict]` — scroll with `should=[a_id==user, b_id==user]` (ScrollFilter), python-sort by `weight` desc, truncate to limit.
- `async def filter_existing_message_ids(chat_id: int, message_ids: list[int]) -> set[int]` — dedup guard:
  compute point ids, chunk 256, `retrieve(..., with_vectors=False)` per chunk, return the subset of
  `message_ids` whose points already exist.
- `async def collection_stats() -> dict` — for all four collections (messages, chats, profiles, relations):
  `{"points": int | None, "exists": bool}`; plus `"messages_usage": {"max_vectors": MAX_VECTORS,
  "usage_ratio": points/MAX_VECTORS or 0.0}` and `"total_points"`. Use one `get_collections()` call +
  one `get_collection(name)` per collection; a missing collection → `exists: False, points: None`.

## Notes

- All returned payloads must be JSON-safe (ISO date strings, plain scalars/containers).
- `updated_at` = `datetime.now(timezone.utc).isoformat()`.
- Do not add `load_dotenv()` — `config` already does it.
