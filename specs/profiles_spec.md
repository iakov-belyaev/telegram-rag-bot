# Spec: `profiles.py` — OSINT profile building + network/relation mapping

Replace the stub in `profiles.py`. Python 3.11, full type hints, no prints at import.
Runtime logging via a local `log()` (same timestamped format as scraper.py).

Data source of truth: the `telegram_messages` collection (decoupled from scraper —
works on whatever is already indexed). Profile enrichment (bio, photo) uses the
Pyrogram userbot via the shared FloodGate/RateGate primitives imported from
`scraper.py`.

## Imports

```python
import asyncio
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from pyrogram import errors as tg_errors

import config
import database as db
from scraper import FloodGate, RateGate, telegram_call
```

Plus the same timestamped `log(message: str)` helper as scraper.py
(`[profiles HH:MM:SS]` prefix).

## 1. Pure helpers (unit-testable, no network)

- `MENTION_REGEX = re.compile(r"@([A-Za-z0-9_]{4,32})")`
- `def _parse_dt(value: str) -> Optional[datetime]:` — tolerant: try
  `datetime.fromisoformat`, then a few fallbacks (`"%Y-%m-%d %H:%M:%S%z"`,
  `"%Y-%m-%d %H:%M:%S"` as UTC); return None if unparseable.
- `def _pair(a: int, b: int) -> tuple[int, int]:` — sorted tuple.
- `def compute_common_chats(profile_a: dict, profile_b: dict) -> dict:` — returns
  `{"chat_ids": sorted intersection of common_chat_ids, "titles": titles for those ids}`
  (titles looked up from whichever profile has them mapped by index when lengths match,
  else return []).

## 2. Aggregation from indexed messages

```python
async def aggregate_message_stats(gate: FloodGate) -> dict:
    """Scroll the whole messages collection and aggregate per-sender stats.

    Returns:
    {
      "users": {
        int(user_id): {
            "message_count": int,
            "username": str, "display_name": str,          # latest non-empty wins
            "first_seen": str, "last_seen": str,           # ISO strings (raw payload dates, min/max)
            "chats": {int(chat_id): {"count": int, "title": str}},
            "reply_targets": {int(user_id): {"count": int, "chats": set[int]}},
            "mentions": {str(username_lower): {"count": int, "chats": set[int]}},
        }, ...
      },
      "chat_members": {int(chat_id): {int(user_id): message_count}},
      "username_map": {str(username_lower): int(user_id)},   # for mention resolution
      "scanned_points": int,
    }

    Rules:
    - Scroll in pages of config.PROFILE_SCROLL_PAGE using db.get_qdrant().scroll with
      payload fields only (with_vectors=False), paginating with next_page_offset
      until exhausted. Wrap each scroll page with a retry/thread call — reuse
      db._qdrant_call if exported, otherwise local retry identical to database.py's.
    - SKIP points whose sender_id == chat_id (broadcast channels).
    - Dates compared with _parse_dt for min/max; fall back to plain string compare.
    - username_map: only senders with a non-empty sender_username.
    - Mentions extracted from payload "text" per message; only usernames that resolve
      via username_map count — do that resolution AFTER the scroll (second pass over
      collected raw mention hits) to handle usernames seen later.
    """
```
Design note: collect raw per-message tuples during the scroll is too much memory for
large corpora — instead during the scroll, for each message: increment counters, and
store `mentions_raw: {msg_sender_id: {username: count}}` plus chat sets; resolve to
user_ids at the end via username_map. Keep it lean.

## 3. Profile enrichment (Telegram user objects + photo hash)

```python
async def enrich_user(client, gate: FloodGate, user_id: int,
                      existing: Optional[dict]) -> dict:
    """Fetch fresh Telegram user info + profile-photo hash.

    Freshness: if `existing` is not None and its "updated_at" parses within
    config.PROFILE_REFRESH_INTERVAL_SECONDS → return
    {"bio": existing.get("bio", ""), "first_name": ..., "last_name": ...,
     "username": ..., "is_bot": ..., "is_premium": ...,
     "pfp_hash": existing.get("pfp_hash", ""), "pfp_meta": existing.get("pfp_meta", {}),
     "refreshed": False} without hitting the network.

    Otherwise:
    - user = await telegram_call(gate, client.get_users, user_id)
      (on Exception → fall back to a minimal dict with refreshed=False; never crash the sweep)
    - bio from getattr(user, "bio", "") or ""
    - pfp: photos = await telegram_call(gate, client.get_profile_photos, user_id, limit=1);
      if photos: pick the SMALLEST PhotoSize in photos[0] (min by file_size or (width*height));
      blob = await telegram_call(gate, client.download_media, chosen, in_memory=True);
      bytes → sha256 hexdigest truncated to config.PFP_HASH_SIZE // 4 chars;
      pfp_meta = {"width": w, "height": h, "file_size": fs}; all wrapped in try/except →
      ("", {}) on failure.
    - returns dict with refreshed=True.
    """
```

## 4. Relation building (pure assembly + upsert)

```python
def build_relations(stats: dict) -> list[tuple[dict, list[float]]] ... 
```
Actually two steps:

```python
def assemble_relation_payloads(stats: dict) -> list[dict]:
    """Build relation payload dicts (no embeddings) from aggregation stats.

    Inputs: stats["users"], stats["chat_members"].
    - co_member edges: for each chat, consider its top config.RELATION_MAX_USERS_PER_CHAT
      members by message_count (only users present in stats["users"]); all unique pairs;
      accumulate: shared_chats set, weight contribution 1 per shared chat, min first_seen /
      max last_seen of the two users' chats.
    - replies edges: for user u, u["reply_targets"][cp] where cp is also a qualifying user:
      weight = count; evidence chats = the recorded reply chats.
    - mentions edges: from resolved mentions {user_id: {"count": int, "chats": set}}:
      weight = count; evidence chats.
    - Output payloads via db.build_relation_payload(...) with:
      kind="co_member"|"replies"|"mentions",
      weights: co_member = number of shared chats; replies/mentions = interaction count,
      evidence_chat_ids = sorted evidence set,
      first_seen/last_seen = the pair's min/max ISO strings ("" if unknown).
    - Drop co_member edges below config.RELATION_MIN_SHARED_CHATS shared chats.
    - Deduplicate edges of the same (pair, kind) by summing weights and merging evidence
      sets. Cap total edges at 20000 (safety) keeping highest weights.
    """
```

```python
async def upsert_relations_batch(payloads: list[dict]) -> int:
    """Embed each payload's db.build_relation_embed_text text and upsert via
    db.upsert_relations([(payload, vector), ...]); returns count."""
```

## 5. Full sweep

```python
async def build_profiles(client, gate: FloodGate, rate: RateGate) -> dict:
    """One full profile-building sweep. Returns JSON-safe stats:
    {"scanned_points": n, "qualifying_users": n, "profiles_upserted": n,
     "relations_upserted": n, "errors": n, "duration_seconds": float}"""
```
Flow:
1. `stats = await aggregate_message_stats(gate)`
2. qualifying = users with message_count >= config.PROFILE_MIN_MESSAGES, sorted by
   message_count desc, capped at config.PROFILE_MAX_USERS_PER_SWEEP.
3. For each qualifying user (sequentially, rate.acquire() before each network call;
   per-user try/except counting errors):
   - `existing = await db.get_profile(uid)`
   - `info = await enrich_user(client, gate, uid, existing)`
   - payload = `db.build_profile_payload(...)` with:
     username/first/last from info (fallback to aggregated names), bio, pfp_hash,
     pfp_meta, common_chat_ids/titles from u["chats"] (titles: use stored chat titles),
     message_count, interactions = top config.PROFILE_TOP_INTERACTIONS entries of
     reply_targets as {str(cp): count}, first_seen/last_seen.
   - embed text via db.build_profile_embed_text → collect (uid, payload, vector)
   - flush in batches of 16 via db.upsert_profiles at the end (or every 16 users).
4. `payloads = assemble_relation_payloads(stats)` → `upsert_relations_batch(payloads)`.
5. Log progress every 25 users; return stats dict.

## 6. Background loop + query helpers

```python
async def profile_sweep(client, control) -> None:
    """Forever: build_profiles; then sleep config.PROFILE_REFRESH_INTERVAL_SECONDS,
    checking control.stop_requested/control.paused every 5s. Uses control.gate if set
    else a fresh FloodGate; builds its own RateGate(config.TG_OPS_PER_SECOND,
    config.TG_OPS_BURST). Runs one sweep ~30s after startup (asyncio.sleep(30)) so the
    scraper gets settled first."""

async def whois(user_id: int) -> Optional[dict]:
    """db.get_profile(uid) + db.get_relations_for(uid) → {"profile": ..., "relations": [...]} or None."""

async def search_users(query: str, limit: int = 10) -> list[dict]:
    """db.search_profiles(query, limit) → payloads list."""

async def introduce(user_a_id: int, user_b_id: int) -> dict:
    """Matchmaking payload for bot.py:
    {"a": profile_a|None, "b": profile_b|None,
     "common_chats": compute_common_chats(a, b) when both exist else {"chat_ids": [], "titles": []},
     "relation": the specific relation payload between the pair if present (scan
       db.get_relations_for(a) for b_id match), else None}"""
```

## 7. Notes

- The sweep must never raise out of profile_sweep (wrap each phase, log, continue).
- All counts/ids in returned dicts must be JSON-safe (no sets — convert to sorted lists).
- Use `asyncio.to_thread`/db helpers for anything blocking; never block the loop.
