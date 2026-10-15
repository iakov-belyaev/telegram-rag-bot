# Fixups round 1 — edit ONLY `database.py` and `config.py`

Apply EXACTLY these changes. Nothing else. Standard SEARCH/REPLACE blocks only.

## A. database.py — graceful reads when a collection does not exist yet

1. Add this import right after the existing qdrant_client.models import block:

```python
from qdrant_client.http.exceptions import UnexpectedResponse
```

2. Add these two helpers immediately AFTER the existing `_qdrant_call` function:

```python
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
```

3. In `search_messages`: replace the `response = await _qdrant_call(...)` call with
`response = await _qdrant_read(None, get_qdrant().query_points, ...)` (same kwargs) and change the tail to:
```python
    if response is None:
        return []
    return response.points
```

4. In `search_profiles`: same treatment (`_qdrant_read(None, ...)`, return [] if None).

5. In `get_profile`: use `records = await _qdrant_read([], get_qdrant().retrieve, ...)` (default empty list);
keep the existing `if not records: return None` tail.

6. In `get_profiles_bulk`: inside the chunk loop, use `records = await _qdrant_read([], get_qdrant().retrieve, ...)`
so a missing collection yields `{}` overall.

7. In `get_relations_for`: use `records, _ = await _qdrant_read(([], None), get_qdrant().scroll, ...)`;
keep the rest unchanged.

8. In `filter_existing_message_ids`: inside the chunk loop, use `records = await _qdrant_read([], get_qdrant().retrieve, ...)`.

9. In `build_message_payload`: compute reply target extraction just before the `return {`:

```python
    reply = getattr(message, "reply_to_message", None)
    reply_to_user_id = getattr(getattr(reply, "from_user", None), "id", None) if reply else None
    reply_to_message_id = getattr(reply, "id", None) if reply else None
```

and add these two keys at the END of the returned dict (after "link"):

```python
        "reply_to_user_id": reply_to_user_id,
        "reply_to_message_id": reply_to_message_id,
```

## B. config.py — new constants + summary entries

10. Immediately AFTER the `PARALLEL_JOIN_CONCURRENCY` line (end of the FloodWait block), add:

```python
# --- Rate limiting (Telegram API pacing) ---
TG_OPS_PER_SECOND: float = _env_float("TG_OPS_PER_SECOND", 5.0)
TG_OPS_BURST: int = _env_int("TG_OPS_BURST", 5)
```

11. Immediately AFTER the `CASUAL_DEEP_LIMIT` line (end of the Profiles/OSINT block), add:

```python
PROFILE_MIN_MESSAGES: int = _env_int("PROFILE_MIN_MESSAGES", 3)
PROFILE_TOP_INTERACTIONS: int = _env_int("PROFILE_TOP_INTERACTIONS", 50)
PROFILE_SCROLL_PAGE: int = _env_int("PROFILE_SCROLL_PAGE", 1000)
RELATION_MAX_USERS_PER_CHAT: int = _env_int("RELATION_MAX_USERS_PER_CHAT", 20)
```

12. In `config_summary()` dict: add `"tg_ops_per_second": TG_OPS_PER_SECOND,` and
`"tg_ops_burst": TG_OPS_BURST,` after the `"flood_max_retries"` entry; add
`"profile_min_messages": PROFILE_MIN_MESSAGES,`, `"profile_top_interactions": PROFILE_TOP_INTERACTIONS,`,
`"profile_scroll_page": PROFILE_SCROLL_PAGE,`, `"relation_max_users_per_chat": RELATION_MAX_USERS_PER_CHAT,`
after the `"profile_refresh_interval_seconds"` entry.
