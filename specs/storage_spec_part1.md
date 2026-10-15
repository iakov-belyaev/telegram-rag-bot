# Spec PART 1 of 2: `storage.py` — backend abstraction + failover manager core

Pass 1 of 2. Replace the `# STUB_PART_1:` lines ONLY; leave `# STUB_PART_2:` intact.
The module REPLACES `database` as the import target for callers (`import storage as db`)
while delegating Qdrant operations to database.py underneath.

## Preamble

```python
import asyncio
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from qdrant_client.models import PointStruct

import config
import database as qdb           # the Qdrant-native layer (keep name qdb!)
import metrics
from logging_config import get_logger

_logger = get_logger("storage")


def log(message: str) -> None:
    _logger.info("%s", message)
```

## 1. Pure re-exports (no failover needed — builders/ids/embeddings)

Re-export unchanged from `qdb`: `embed_texts, message_point_id, chat_point_id,
profile_point_id, relation_point_id, build_message_payload, build_profile_payload,
build_relation_payload, build_profile_embed_text, build_relation_embed_text,
build_message_link, get_qdrant`. (Assign them as module attributes.)

## 2. SqliteBackend — local mirror store, identical schema semantics

```python
class SqliteBackend:
    """Local SQLite mirror used when Qdrant is unavailable.

    Tables: messages, chats, profiles, relations (payload JSON + dirty flag),
    messages_fts (FTS5 over text/chat_title/sender_name), meta.
    All methods are SYNC (called via asyncio.to_thread by the async wrappers).
    """

    def __init__(self, path: Optional[Path] = None) -> None: ...
```
- Path: `path or config.FAILOVER_SQLITE_PATH`; mkdir parents; open ONE connection
  (`check_same_thread=False`), `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=NORMAL`.
  Guard every connection-creating call with a `threading.Lock` (self._lock).
- `def _ensure_schema(self) -> None` — CREATE TABLE IF NOT EXISTS:
  - messages(point_id TEXT PRIMARY KEY, chat_id INTEGER, message_id INTEGER,
    text TEXT, chat_title TEXT, sender_name TEXT, sender_username TEXT,
    payload TEXT NOT NULL, dirty INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL)
    + indexes on chat_id, dirty.
  - chats(point_id TEXT PRIMARY KEY, chat_id INTEGER, title TEXT, payload TEXT NOT NULL,
    dirty INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL)
  - profiles(point_id TEXT PRIMARY KEY, user_id INTEGER, payload TEXT NOT NULL,
    dirty INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL) + index on dirty
  - relations(point_id TEXT PRIMARY KEY, a_id INTEGER, b_id INTEGER, edge_kind TEXT,
    payload TEXT NOT NULL, dirty INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL)
  - messages_fts USING fts5(text, chat_title, sender_name, content='')
  - meta(key TEXT PRIMARY KEY, value TEXT)
- `def upsert_message_rows(self, rows: list[dict]) -> int` — rows:
  {point_id, chat_id, message_id, text, chat_title, sender_name, sender_username, payload(dict)} →
  INSERT OR REPLACE (json.dumps payload) dirty=1; maintain FTS: DELETE FROM messages_fts WHERE
  rowid=(SELECT rowid FROM messages WHERE point_id=?) then INSERT INTO messages_fts(rowid, text,
  chat_title, sender_name) SELECT rowid, text, chat_title, sender_name FROM messages WHERE
  point_id=?.
- `def upsert_chat_rows(self, rows) -> int` (payload json; title).
- `def upsert_profile_rows(self, rows: list[dict]) -> int` — {point_id, user_id, payload}.
- `def upsert_relation_rows(self, rows: list[dict]) -> int` — {point_id, a_id, b_id, edge_kind, payload}.
- `def search_messages(self, query: str, limit: int = 10, chat_id: Optional[int] = None,
  sender_username: Optional[str] = None) -> list` — FTS5 ranked; sanitize query via
  `_fts_match_query(query)` (tokens [A-Za-z0-9_]+ joined " OR ", each quoted; empty → LIKE
  fallback '%query%'); LEFT JOIN messages for payload; apply chat_id/sender filters;
  wrap each row into `SimpleNamespace(payload=<dict>, score=None, id=point_id)`; order by rank
  then updated_at desc; LIKE fallback when FTS yields nothing.
- `def search_profiles(self, query, limit=10) -> list` — LIKE over payload (username/display/bio) OR
  fts-less scan; return SimpleNamespace(payload=..., score=None).
- `def get_profile(self, user_id) -> Optional[dict]`; `def get_profiles_bulk(self, user_ids) -> dict[int, dict]`
  (payload["user_id"] keys); `def get_relations_for(self, user_id, limit=50) -> list[dict]`
  (a_id or b_id, sorted weight desc).
- `def filter_existing_message_ids(self, chat_id, message_ids) -> set[int]` — SELECT message_id.
- `def counts(self) -> dict` — {messages, chats, profiles, relations, dirty} counts.
- `def fetch_dirty(self, table: str, limit: int) -> list[dict]` — rows where dirty=1 ordered by
  updated_at asc; returns {rowid, point_id, payload(dict)} (+ message text/chat_title/sender_name
  for messages; user_id for profiles; a_id/b_id/edge_kind for relations — extra keys ok).
- `def mark_clean(self, table: str, point_ids: list[str]) -> int` — UPDATE dirty=0.
- `def replace_with_rows(...)` not needed. `def close(self)`.

## 3. QdrantBackend — trivial delegation

```python
class QdrantBackend:
    """Delegates every operation to database.py (Qdrant)."""
```
Methods mirroring: upsert_chat(chat), upsert_messages(points), upsert_profiles(items),
upsert_relations(items), search_messages(...), search_profiles(...), get_profile(uid),
get_profiles_bulk(uids), get_relations_for(uid, limit), filter_existing_message_ids(cid, ids),
collection_stats(), counts() → derive from collection_stats (messages/chats/profiles/relations
points + dirty=0), fetch_dirty → [] , mark_clean → 0 (qdrant needs no sync).
Async methods for all network paths (await qdb.*). `probe()` → `await qdb._qdrant_call(qdb.get_qdrant().get_collections)`.

## 4. FailoverManager

```python
class FailoverManager:
    """Routes storage ops to Qdrant with transparent SQLite failover + resync."""

    def __init__(self, sqlite_path: Optional[Path] = None, enabled: Optional[bool] = None) -> None:
        self.enabled = config.FAILOVER_ENABLED if enabled is None else enabled
        self.mode = "qdrant"                      # "qdrant" | "sqlite"
        self.qdrant = QdrantBackend()
        self.sqlite = SqliteBackend(sqlite_path)
        self.consecutive_errors = 0
        self.degrade_events = 0
        self.last_error = ""
        self.last_probe_ok_at = 0.0
        self.resyncing = False

    def note_success(self) -> None
    def note_error(self, exc: Exception) -> None    # ++errors; >= config.FAILOVER_DEGRADE_ERRORS → _use_sqlite()
    def _use_sqlite(self, reason: str) -> None      # idempotent; degrade_events++; metrics.inc("failover_switches"); log
    def _use_qdrant(self) -> None                   # idempotent ("recovered"); metrics + log

    async def probe_qdrant(self) -> bool:
        """Single health probe with config.FAILOVER_PROBE_TIMEOUT_SECONDS.
        True → note_success(); if mode==sqlite → _use_qdrant() (+ schedules resync via
        asyncio.create_task(resync_pending()) if not already running). False → note_error."""
    def status(self) -> dict:
        """JSON-safe: mode, enabled, consecutive_errors, degrade_events, last_error,
        last_probe_ok_at, resyncing, sqlite counts."""
```

## 5. Async wrapper functions (THE public API — same names as database.py)

Pattern:
```python
async def _qdrant_then_sqlite(q_call, s_call):
    """Try the active path; on Qdrant errors fall back to SQLite (marking dirty)."""
```
- Write wrappers (`upsert_chat`, `upsert_messages`, `upsert_profiles`, `upsert_relations`):
  - manager = get_manager()
  - if not manager.enabled or manager.mode == "qdrant": try qdrant path → manager.note_success();
    return. except → manager.note_error(e); then sqlite path (dirty=1) → log fallback; return count.
  - else (mode sqlite): sqlite path directly.
  - SQLite row builders:
    - `_message_rows(points)` — from PointStruct list: payload = point.payload; text=payload["text"],
      chat_title, sender_name, sender_username, chat_id, message_id; point_id=str(point.id).
    - `_chat_rows(chat)` — payload dict {chat_id, chat_title, chat_type, username, updated_at};
      title = chat.title or chat.first_name or "Private Chat"; point_id = chat_point_id(chat.id).
    - `_profile_rows(items)`, `_relation_rows(items)` — (uid,payload,vector)/(payload,vector) → rows with
      json payloads; point ids via profile_point_id/relation_point_id.
  - All sqlite calls via `await asyncio.to_thread(...)`.
- Read wrappers (`search_messages`, `search_profiles`, `get_profile`, `get_profiles_bulk`,
  `get_relations_for`, `filter_existing_message_ids`): same pattern (try active → fallback on error).
- `async def collection_stats()` — if mode qdrant: try qdb.collection_stats() except → note_error +
  sqlite counts shaped identically (keys = collection names, points counts, exists True,
  messages_usage, total_points...). If sqlite mode: sqlite counts shaped same. ALWAYS add
  `"failover": manager.status()`.

## 6. Singleton accessors

```python
_MANAGER: Optional[FailoverManager] = None

def get_manager() -> FailoverManager:      # lazy build
def reset_manager(manager: Optional[FailoverManager] = None) -> None   # tests
```
