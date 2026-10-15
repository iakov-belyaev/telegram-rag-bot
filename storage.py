"""Storage abstraction with transparent Qdrant→SQLite failover and resync.

Public API mirrors database.py (drop-in: `import storage as db`). When Qdrant is
unreachable/degraded, writes land in a local SQLite mirror (dirty-marked) and
reads are served locally; on recovery a resync uploads the dirty rows back.

Backends are swappable via the FailoverManager (see specs/storage_spec_part1/2).
"""

import asyncio
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from qdrant_client.models import PointStruct

import config
import database as qdb  # the Qdrant-native layer (keep name qdb!)
import metrics
from logging_config import get_logger

_logger = get_logger("storage")


def log(message: str) -> None:
    _logger.info("%s", message)


# ---------------------------------------------------------------------------
# 1. Pure re-exports (no failover needed — builders/ids/embeddings)
# ---------------------------------------------------------------------------
embed_texts = qdb.embed_texts
message_point_id = qdb.message_point_id
chat_point_id = qdb.chat_point_id
profile_point_id = qdb.profile_point_id
relation_point_id = qdb.relation_point_id
build_message_payload = qdb.build_message_payload
build_profile_payload = qdb.build_profile_payload
build_relation_payload = qdb.build_relation_payload
build_profile_embed_text = qdb.build_profile_embed_text
build_relation_embed_text = qdb.build_relation_embed_text
build_message_link = qdb.build_message_link
get_qdrant = qdb.get_qdrant

# Low-level Qdrant helpers re-exported for callers that talk to Qdrant directly
# (e.g. profiles._scroll_page, discovery scans) — keep retry/timeout semantics.
_qdrant_read = qdb._qdrant_read
_qdrant_call = qdb._qdrant_call


# ---------------------------------------------------------------------------
# 2. SqliteBackend — local mirror store, identical schema semantics
# ---------------------------------------------------------------------------
_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _fts_match_query(query: str) -> str:
    """Sanitize a free-text query into an FTS5 MATCH expression.

    Tokens are restricted to [A-Za-z0-9_]+ and joined with OR, each quoted.
    Returns "" when no usable tokens exist (caller should LIKE-fallback).
    """
    tokens = _FTS_TOKEN_RE.findall(query or "")
    if not tokens:
        return ""
    return " OR ".join(f'"{tok}"' for tok in tokens)


class SqliteBackend:
    """Local SQLite mirror used when Qdrant is unavailable.

    Tables: messages, chats, profiles, relations (payload JSON + dirty flag),
    messages_fts (FTS5 over text/chat_title/sender_name), meta.
    All methods are SYNC (called via asyncio.to_thread by the async wrappers).
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or config.FAILOVER_SQLITE_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_schema()

    # -- connection management ------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                conn = sqlite3.connect(str(self.path), check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                self._conn = conn
            return self._conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        with self._lock:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    point_id TEXT PRIMARY KEY,
                    chat_id INTEGER,
                    message_id INTEGER,
                    text TEXT,
                    chat_title TEXT,
                    sender_name TEXT,
                    sender_username TEXT,
                    payload TEXT NOT NULL,
                    dirty INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id);
                CREATE INDEX IF NOT EXISTS idx_messages_dirty ON messages(dirty);

                CREATE TABLE IF NOT EXISTS chats (
                    point_id TEXT PRIMARY KEY,
                    chat_id INTEGER,
                    title TEXT,
                    payload TEXT NOT NULL,
                    dirty INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS profiles (
                    point_id TEXT PRIMARY KEY,
                    user_id INTEGER,
                    payload TEXT NOT NULL,
                    dirty INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_profiles_dirty ON profiles(dirty);

                CREATE TABLE IF NOT EXISTS relations (
                    point_id TEXT PRIMARY KEY,
                    a_id INTEGER,
                    b_id INTEGER,
                    edge_kind TEXT,
                    payload TEXT NOT NULL,
                    dirty INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                    text, chat_title, sender_name, content=''
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            conn.commit()

    # -- writes ---------------------------------------------------------------
    def upsert_message_rows(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        conn = self._connect()
        now = time.time()
        with self._lock:
            for row in rows:
                point_id = row["point_id"]
                conn.execute(
                    """
                    INSERT OR REPLACE INTO messages
                        (point_id, chat_id, message_id, text, chat_title,
                         sender_name, sender_username, payload, dirty, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        point_id,
                        row.get("chat_id"),
                        row.get("message_id"),
                        row.get("text", ""),
                        row.get("chat_title", ""),
                        row.get("sender_name", ""),
                        row.get("sender_username", ""),
                        json.dumps(row.get("payload", {})),
                        now,
                    ),
                )
                conn.execute(
                    "DELETE FROM messages_fts WHERE rowid = "
                    "(SELECT rowid FROM messages WHERE point_id = ?)",
                    (point_id,),
                )
                conn.execute(
                    """
                    INSERT INTO messages_fts(rowid, text, chat_title, sender_name)
                    SELECT rowid, text, chat_title, sender_name
                    FROM messages WHERE point_id = ?
                    """,
                    (point_id,),
                )
            conn.commit()
        return len(rows)

    def upsert_chat_rows(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        conn = self._connect()
        now = time.time()
        with self._lock:
            for row in rows:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO chats
                        (point_id, chat_id, title, payload, dirty, updated_at)
                    VALUES (?, ?, ?, ?, 1, ?)
                    """,
                    (
                        row["point_id"],
                        row.get("chat_id"),
                        row.get("title", ""),
                        json.dumps(row.get("payload", {})),
                        now,
                    ),
                )
            conn.commit()
        return len(rows)

    def upsert_profile_rows(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        conn = self._connect()
        now = time.time()
        with self._lock:
            for row in rows:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO profiles
                        (point_id, user_id, payload, dirty, updated_at)
                    VALUES (?, ?, ?, 1, ?)
                    """,
                    (
                        row["point_id"],
                        row.get("user_id"),
                        json.dumps(row.get("payload", {})),
                        now,
                    ),
                )
            conn.commit()
        return len(rows)

    def upsert_relation_rows(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        conn = self._connect()
        now = time.time()
        with self._lock:
            for row in rows:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO relations
                        (point_id, a_id, b_id, edge_kind, payload, dirty, updated_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        row["point_id"],
                        row.get("a_id"),
                        row.get("b_id"),
                        row.get("edge_kind", ""),
                        json.dumps(row.get("payload", {})),
                        now,
                    ),
                )
            conn.commit()
        return len(rows)

    # -- reads ----------------------------------------------------------------
    def search_messages(
        self,
        query: str,
        limit: int = 10,
        chat_id: Optional[int] = None,
        sender_username: Optional[str] = None,
    ) -> list:
        conn = self._connect()
        match = _fts_match_query(query)
        results: list = []
        with self._lock:
            if match:
                sql = (
                    "SELECT m.point_id AS point_id, m.payload AS payload, "
                    "m.updated_at AS updated_at, bm25(messages_fts) AS rank "
                    "FROM messages_fts "
                    "JOIN messages m ON m.rowid = messages_fts.rowid "
                    "WHERE messages_fts MATCH ?"
                )
                params: list[Any] = [match]
                if chat_id is not None:
                    sql += " AND m.chat_id = ?"
                    params.append(chat_id)
                if sender_username is not None:
                    sql += " AND m.sender_username = ?"
                    params.append(sender_username)
                sql += " ORDER BY rank ASC, m.updated_at DESC LIMIT ?"
                params.append(limit)
                try:
                    rows = conn.execute(sql, params).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                results = [
                    SimpleNamespace(
                        payload=json.loads(r["payload"]),
                        score=None,
                        id=r["point_id"],
                    )
                    for r in rows
                ]
            if not results:
                like = f"%{query}%"
                sql = (
                    "SELECT point_id, payload FROM messages "
                    "WHERE (text LIKE ? OR chat_title LIKE ? OR sender_name LIKE ?)"
                )
                params = [like, like, like]
                if chat_id is not None:
                    sql += " AND chat_id = ?"
                    params.append(chat_id)
                if sender_username is not None:
                    sql += " AND sender_username = ?"
                    params.append(sender_username)
                sql += " ORDER BY updated_at DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, params).fetchall()
                results = [
                    SimpleNamespace(
                        payload=json.loads(r["payload"]),
                        score=None,
                        id=r["point_id"],
                    )
                    for r in rows
                ]
        return results

    def search_profiles(self, query: str, limit: int = 10) -> list:
        conn = self._connect()
        like = f"%{query}%"
        with self._lock:
            rows = conn.execute(
                """
                SELECT point_id, payload FROM profiles
                WHERE payload LIKE ?
                ORDER BY updated_at DESC LIMIT ?
                """,
                (like, limit),
            ).fetchall()
        return [
            SimpleNamespace(payload=json.loads(r["payload"]), score=None, id=r["point_id"])
            for r in rows
        ]

    def get_profile(self, user_id: int) -> Optional[dict]:
        conn = self._connect()
        with self._lock:
            row = conn.execute(
                "SELECT payload FROM profiles WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload"])

    def get_profiles_bulk(self, user_ids: list[int]) -> dict[int, dict]:
        result: dict[int, dict] = {}
        if not user_ids:
            return result
        conn = self._connect()
        with self._lock:
            for uid in user_ids:
                row = conn.execute(
                    "SELECT payload FROM profiles WHERE user_id = ? LIMIT 1",
                    (uid,),
                ).fetchone()
                if row is not None:
                    payload = json.loads(row["payload"])
                    result[payload.get("user_id", uid)] = payload
        return result

    def get_relations_for(self, user_id: int, limit: int = 50) -> list[dict]:
        conn = self._connect()
        with self._lock:
            rows = conn.execute(
                """
                SELECT payload FROM relations
                WHERE a_id = ? OR b_id = ?
                """,
                (user_id, user_id),
            ).fetchall()
        payloads = [json.loads(r["payload"]) for r in rows]
        payloads.sort(key=lambda p: p.get("weight", 0), reverse=True)
        return payloads[:limit]

    def filter_existing_message_ids(
        self, chat_id: int, message_ids: list[int]
    ) -> set[int]:
        if not message_ids:
            return set()
        conn = self._connect()
        existing: set[int] = set()
        with self._lock:
            for mid in message_ids:
                row = conn.execute(
                    "SELECT 1 FROM messages WHERE chat_id = ? AND message_id = ? LIMIT 1",
                    (chat_id, mid),
                ).fetchone()
                if row is not None:
                    existing.add(mid)
        return existing

    def counts(self) -> dict:
        conn = self._connect()
        with self._lock:
            def _count(table: str) -> int:
                row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
                return int(row["c"]) if row else 0

            dirty = 0
            for table in ("messages", "chats", "profiles", "relations"):
                row = conn.execute(
                    f"SELECT COUNT(*) AS c FROM {table} WHERE dirty = 1"
                ).fetchone()
                dirty += int(row["c"]) if row else 0
            return {
                "messages": _count("messages"),
                "chats": _count("chats"),
                "profiles": _count("profiles"),
                "relations": _count("relations"),
                "dirty": dirty,
            }

    def fetch_dirty(self, table: str, limit: int) -> list[dict]:
        if table not in ("messages", "chats", "profiles", "relations"):
            return []
        conn = self._connect()
        with self._lock:
            rows = conn.execute(
                f"SELECT rowid AS rowid, * FROM {table} WHERE dirty = 1 "
                "ORDER BY updated_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            item: dict[str, Any] = {
                "rowid": r["rowid"],
                "point_id": r["point_id"],
                "payload": json.loads(r["payload"]),
            }
            if table == "messages":
                item["text"] = r["text"]
                item["chat_title"] = r["chat_title"]
                item["sender_name"] = r["sender_name"]
                item["chat_id"] = r["chat_id"]
                item["message_id"] = r["message_id"]
            elif table == "profiles":
                item["user_id"] = r["user_id"]
            elif table == "relations":
                item["a_id"] = r["a_id"]
                item["b_id"] = r["b_id"]
                item["edge_kind"] = r["edge_kind"]
            out.append(item)
        return out

    def mark_clean(self, table: str, point_ids: list[str]) -> int:
        if table not in ("messages", "chats", "profiles", "relations") or not point_ids:
            return 0
        conn = self._connect()
        with self._lock:
            updated = 0
            for pid in point_ids:
                cur = conn.execute(
                    f"UPDATE {table} SET dirty = 0 WHERE point_id = ?",
                    (pid,),
                )
                updated += cur.rowcount or 0
            conn.commit()
        return updated

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None


# ---------------------------------------------------------------------------
# 3. QdrantBackend — trivial delegation
# ---------------------------------------------------------------------------
class QdrantBackend:
    """Delegates every operation to database.py (Qdrant)."""

    async def upsert_chat(self, chat: Any) -> None:
        await qdb.upsert_chat(chat)

    async def upsert_messages(self, points: list[PointStruct]) -> int:
        return await qdb.upsert_messages(points)

    async def upsert_profiles(self, items: list[tuple[int, dict, list[float]]]) -> int:
        return await qdb.upsert_profiles(items)

    async def upsert_relations(self, items: list[tuple[dict, list[float]]]) -> int:
        return await qdb.upsert_relations(items)

    async def search_messages(
        self,
        query: str,
        limit: int = 10,
        chat_id: Optional[int] = None,
        sender_username: Optional[str] = None,
        score_threshold: Optional[float] = None,
    ) -> list:
        return await qdb.search_messages(
            query,
            limit=limit,
            chat_id=chat_id,
            sender_username=sender_username,
            score_threshold=score_threshold,
        )

    async def search_profiles(self, query: str, limit: int = 10) -> list:
        return await qdb.search_profiles(query, limit=limit)

    async def get_profile(self, user_id: int) -> Optional[dict]:
        return await qdb.get_profile(user_id)

    async def get_profiles_bulk(self, user_ids: list[int]) -> dict[int, dict]:
        return await qdb.get_profiles_bulk(user_ids)

    async def get_relations_for(self, user_id: int, limit: int = 50) -> list[dict]:
        return await qdb.get_relations_for(user_id, limit=limit)

    async def filter_existing_message_ids(
        self, chat_id: int, message_ids: list[int]
    ) -> set[int]:
        return await qdb.filter_existing_message_ids(chat_id, message_ids)

    async def collection_stats(self) -> dict:
        return await qdb.collection_stats()

    async def counts(self) -> dict:
        stats = await qdb.collection_stats()
        return {
            "messages": stats.get(qdb.MESSAGES_COLLECTION, {}).get("points") or 0,
            "chats": stats.get(qdb.CHATS_COLLECTION, {}).get("points") or 0,
            "profiles": stats.get(qdb.PROFILES_COLLECTION, {}).get("points") or 0,
            "relations": stats.get(qdb.RELATIONS_COLLECTION, {}).get("points") or 0,
            "dirty": 0,
        }

    def fetch_dirty(self, table: str, limit: int) -> list[dict]:
        return []

    def mark_clean(self, table: str, point_ids: list[str]) -> int:
        return 0

    async def probe(self) -> Any:
        return await qdb._qdrant_call(qdb.get_qdrant().get_collections)


# ---------------------------------------------------------------------------
# 4. FailoverManager
# ---------------------------------------------------------------------------
class FailoverManager:
    """Routes storage ops to Qdrant with transparent SQLite failover + resync."""

    def __init__(
        self, sqlite_path: Optional[Path] = None, enabled: Optional[bool] = None
    ) -> None:
        self.enabled = config.FAILOVER_ENABLED if enabled is None else enabled
        self.mode = "qdrant"                      # "qdrant" | "sqlite"
        self.qdrant = QdrantBackend()
        self.sqlite = SqliteBackend(sqlite_path)
        self.consecutive_errors = 0
        self.degrade_events = 0
        self.last_error = ""
        self.last_probe_ok_at = 0.0
        self.resyncing = False

    def note_success(self) -> None:
        self.consecutive_errors = 0

    def note_error(self, exc: Exception) -> None:
        self.consecutive_errors += 1
        self.last_error = f"{type(exc).__name__}: {exc}"
        threshold = getattr(config, "FAILOVER_DEGRADE_ERRORS", 3)
        if self.enabled and self.consecutive_errors >= threshold:
            self._use_sqlite(self.last_error)

    def _use_sqlite(self, reason: str) -> None:
        if self.mode == "sqlite":
            return
        self.mode = "sqlite"
        self.degrade_events += 1
        metrics.inc("failover_switches")
        log(f"failover: switching to SQLite mirror ({reason})")

    def _use_qdrant(self) -> None:
        # Fresh start after recovery: a stale error count would instantly
        # re-degrade on the first resync hiccup.
        self.consecutive_errors = 0
        if self.mode == "qdrant":
            return
        self.mode = "qdrant"
        metrics.inc("failover_recoveries")
        log("failover: recovered, switching back to Qdrant")

    async def probe_qdrant(self) -> bool:
        """Single health probe with config.FAILOVER_PROBE_TIMEOUT_SECONDS."""
        timeout = getattr(config, "FAILOVER_PROBE_TIMEOUT_SECONDS", 5.0)
        try:
            await asyncio.wait_for(self.qdrant.probe(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            self.note_error(exc)
            return False
        self.note_success()
        self.last_probe_ok_at = time.time()
        if self.mode == "sqlite":
            self._use_qdrant()
            if not self.resyncing:
                resync = globals().get("resync_pending")
                if resync is not None:
                    asyncio.create_task(resync())
        return True

    def status(self) -> dict:
        try:
            sqlite_counts = self.sqlite.counts()
        except Exception:  # noqa: BLE001
            sqlite_counts = {}
        return {
            "mode": self.mode,
            "enabled": self.enabled,
            "consecutive_errors": self.consecutive_errors,
            "degrade_events": self.degrade_events,
            "last_error": self.last_error,
            "last_probe_ok_at": self.last_probe_ok_at,
            "resyncing": self.resyncing,
            "sqlite": sqlite_counts,
        }


# ---------------------------------------------------------------------------
# 5. Async wrapper functions (THE public API — same names as database.py)
# ---------------------------------------------------------------------------
def _message_rows(points: list[PointStruct]) -> list[dict]:
    rows: list[dict] = []
    for point in points:
        payload = point.payload or {}
        rows.append(
            {
                "point_id": str(point.id),
                "chat_id": payload.get("chat_id"),
                "message_id": payload.get("message_id"),
                "text": payload.get("text", ""),
                "chat_title": payload.get("chat_title", ""),
                "sender_name": payload.get("sender_name", ""),
                "sender_username": payload.get("sender_username", ""),
                "payload": payload,
            }
        )
    return rows


def _chat_rows(chat: Any) -> list[dict]:
    title = chat.title or chat.first_name or "Private Chat"
    payload = {
        "chat_id": chat.id,
        "chat_title": title,
        "chat_type": str(chat.type),
        "username": chat.username or "",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return [
        {
            "point_id": chat_point_id(chat.id),
            "chat_id": chat.id,
            "title": title,
            "payload": payload,
        }
    ]


def _profile_rows(items: list[tuple[int, dict, list[float]]]) -> list[dict]:
    return [
        {
            "point_id": profile_point_id(user_id),
            "user_id": user_id,
            "payload": payload,
        }
        for user_id, payload, _vector in items
    ]


def _relation_rows(items: list[tuple[dict, list[float]]]) -> list[dict]:
    rows: list[dict] = []
    for payload, _vector in items:
        rows.append(
            {
                "point_id": relation_point_id(
                    payload["a_id"], payload["b_id"], payload["edge_kind"]
                ),
                "a_id": payload.get("a_id"),
                "b_id": payload.get("b_id"),
                "edge_kind": payload.get("edge_kind", ""),
                "payload": payload,
            }
        )
    return rows


async def _qdrant_then_sqlite(q_call, s_call):
    """Try the active path; on Qdrant errors fall back to SQLite (marking dirty)."""
    manager = get_manager()
    if not manager.enabled or manager.mode == "qdrant":
        try:
            result = await q_call()
            manager.note_success()
            return result
        except Exception as exc:  # noqa: BLE001
            manager.note_error(exc)
            log(f"storage: qdrant path failed, using sqlite fallback ({exc})")
    return await s_call()


async def upsert_chat(chat: Any) -> None:
    manager = get_manager()
    await _qdrant_then_sqlite(
        lambda: manager.qdrant.upsert_chat(chat),
        lambda: asyncio.to_thread(manager.sqlite.upsert_chat_rows, _chat_rows(chat)),
    )


async def upsert_messages(points: list[PointStruct]) -> int:
    manager = get_manager()
    return await _qdrant_then_sqlite(
        lambda: manager.qdrant.upsert_messages(points),
        lambda: asyncio.to_thread(
            manager.sqlite.upsert_message_rows, _message_rows(points)
        ),
    )


async def upsert_profiles(items: list[tuple[int, dict, list[float]]]) -> int:
    manager = get_manager()
    return await _qdrant_then_sqlite(
        lambda: manager.qdrant.upsert_profiles(items),
        lambda: asyncio.to_thread(
            manager.sqlite.upsert_profile_rows, _profile_rows(items)
        ),
    )


async def upsert_relations(items: list[tuple[dict, list[float]]]) -> int:
    manager = get_manager()
    return await _qdrant_then_sqlite(
        lambda: manager.qdrant.upsert_relations(items),
        lambda: asyncio.to_thread(
            manager.sqlite.upsert_relation_rows, _relation_rows(items)
        ),
    )


async def search_messages(
    query: str,
    limit: int = 10,
    chat_id: Optional[int] = None,
    sender_username: Optional[str] = None,
    score_threshold: Optional[float] = None,
) -> list:
    manager = get_manager()
    return await _qdrant_then_sqlite(
        lambda: manager.qdrant.search_messages(
            query,
            limit=limit,
            chat_id=chat_id,
            sender_username=sender_username,
            score_threshold=score_threshold,
        ),
        lambda: asyncio.to_thread(
            manager.sqlite.search_messages,
            query,
            limit,
            chat_id,
            sender_username,
        ),
    )


async def search_profiles(query: str, limit: int = 10) -> list:
    manager = get_manager()
    return await _qdrant_then_sqlite(
        lambda: manager.qdrant.search_profiles(query, limit=limit),
        lambda: asyncio.to_thread(manager.sqlite.search_profiles, query, limit),
    )


async def get_profile(user_id: int) -> Optional[dict]:
    manager = get_manager()
    return await _qdrant_then_sqlite(
        lambda: manager.qdrant.get_profile(user_id),
        lambda: asyncio.to_thread(manager.sqlite.get_profile, user_id),
    )


async def get_profiles_bulk(user_ids: list[int]) -> dict[int, dict]:
    manager = get_manager()
    return await _qdrant_then_sqlite(
        lambda: manager.qdrant.get_profiles_bulk(user_ids),
        lambda: asyncio.to_thread(manager.sqlite.get_profiles_bulk, user_ids),
    )


async def get_relations_for(user_id: int, limit: int = 50) -> list[dict]:
    manager = get_manager()
    return await _qdrant_then_sqlite(
        lambda: manager.qdrant.get_relations_for(user_id, limit=limit),
        lambda: asyncio.to_thread(manager.sqlite.get_relations_for, user_id, limit),
    )


async def filter_existing_message_ids(
    chat_id: int, message_ids: list[int]
) -> set[int]:
    manager = get_manager()
    return await _qdrant_then_sqlite(
        lambda: manager.qdrant.filter_existing_message_ids(chat_id, message_ids),
        lambda: asyncio.to_thread(
            manager.sqlite.filter_existing_message_ids, chat_id, message_ids
        ),
    )


def _sqlite_stats(manager: FailoverManager) -> dict:
    counts = manager.sqlite.counts()
    stats: dict[str, Any] = {
        qdb.MESSAGES_COLLECTION: {"points": counts["messages"], "exists": True},
        qdb.CHATS_COLLECTION: {"points": counts["chats"], "exists": True},
        qdb.PROFILES_COLLECTION: {"points": counts["profiles"], "exists": True},
        qdb.RELATIONS_COLLECTION: {"points": counts["relations"], "exists": True},
    }
    max_vectors = config.MAX_VECTORS
    stats["messages_usage"] = {
        "max_vectors": max_vectors,
        "usage_ratio": (counts["messages"] / max_vectors) if max_vectors else 0.0,
    }
    stats["total_points"] = (
        counts["messages"] + counts["chats"] + counts["profiles"] + counts["relations"]
    )
    return stats


async def collection_stats() -> dict:
    manager = get_manager()
    if not manager.enabled or manager.mode == "qdrant":
        try:
            stats = await manager.qdrant.collection_stats()
            manager.note_success()
        except Exception as exc:  # noqa: BLE001
            manager.note_error(exc)
            log(f"storage: collection_stats failed, using sqlite stats ({exc})")
            stats = _sqlite_stats(manager)
    else:
        stats = _sqlite_stats(manager)
    stats["failover"] = manager.status()
    return stats


# ---------------------------------------------------------------------------
# 6. Singleton accessors
# ---------------------------------------------------------------------------
_MANAGER: Optional[FailoverManager] = None


def get_manager() -> FailoverManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = FailoverManager()
    return _MANAGER


def reset_manager(manager: Optional[FailoverManager] = None) -> None:
    global _MANAGER
    _MANAGER = manager

# ---------------------------------------------------------------------------
# 7. Resync (SQLite → Qdrant when recovered)
# ---------------------------------------------------------------------------
def _chat_embed_text(payload: dict) -> str:
    """Build the chat embed text exactly like database.upsert_chat does."""
    title = payload.get("chat_title", "") or ""
    username = payload.get("username", "") or ""
    text = f"Group: {title}"
    if username:
        text += f" (@{username})"
    return text


async def _resync_messages(manager: FailoverManager, stats: dict) -> bool:
    """Resync one batch of dirty messages. Returns True if a batch was processed."""
    rows = await asyncio.to_thread(
        manager.sqlite.fetch_dirty, "messages", config.FAILOVER_RESYNC_BATCH
    )
    if not rows:
        return False

    texts = [row.get("text", "") or "" for row in rows]
    vectors = await qdb.embed_texts(texts)
    points = [
        PointStruct(id=row["point_id"], vector=vector, payload=row["payload"])
        for row, vector in zip(rows, vectors, strict=True)
    ]
    await qdb.upsert_messages(points)
    point_ids = [row["point_id"] for row in rows]
    await asyncio.to_thread(manager.sqlite.mark_clean, "messages", point_ids)
    metrics.inc("failover_resync_uploaded", len(rows))
    stats["messages"] += len(rows)
    return True


async def _resync_chats(manager: FailoverManager, stats: dict) -> bool:
    """Resync one batch of dirty chats. Returns True if a batch was processed."""
    rows = await asyncio.to_thread(
        manager.sqlite.fetch_dirty, "chats", config.FAILOVER_RESYNC_BATCH
    )
    if not rows:
        return False

    texts = [_chat_embed_text(row["payload"]) for row in rows]
    vectors = await qdb.embed_texts(texts)
    points = [
        PointStruct(id=row["point_id"], vector=vector, payload=row["payload"])
        for row, vector in zip(rows, vectors, strict=True)
    ]
    await qdb._qdrant_call(
        qdb.get_qdrant().upsert,
        collection_name=qdb.CHATS_COLLECTION,
        points=points,
    )
    point_ids = [row["point_id"] for row in rows]
    await asyncio.to_thread(manager.sqlite.mark_clean, "chats", point_ids)
    metrics.inc("failover_resync_uploaded", len(rows))
    stats["chats"] += len(rows)
    return True


async def _resync_profiles(manager: FailoverManager, stats: dict) -> bool:
    """Resync one batch of dirty profiles. Returns True if a batch was processed."""
    rows = await asyncio.to_thread(
        manager.sqlite.fetch_dirty, "profiles", config.FAILOVER_RESYNC_BATCH
    )
    if not rows:
        return False

    texts = [qdb.build_profile_embed_text(row["payload"]) for row in rows]
    vectors = await qdb.embed_texts(texts)
    items = [
        (row["user_id"], row["payload"], vector)
        for row, vector in zip(rows, vectors, strict=True)
    ]
    await qdb.upsert_profiles(items)
    point_ids = [row["point_id"] for row in rows]
    await asyncio.to_thread(manager.sqlite.mark_clean, "profiles", point_ids)
    metrics.inc("failover_resync_uploaded", len(rows))
    stats["profiles"] += len(rows)
    return True


async def _resync_relations(manager: FailoverManager, stats: dict) -> bool:
    """Resync one batch of dirty relations. Returns True if a batch was processed."""
    rows = await asyncio.to_thread(
        manager.sqlite.fetch_dirty, "relations", config.FAILOVER_RESYNC_BATCH
    )
    if not rows:
        return False

    texts = [qdb.build_relation_embed_text(row["payload"]) for row in rows]
    vectors = await qdb.embed_texts(texts)
    items = [(row["payload"], vector) for row, vector in zip(rows, vectors, strict=True)]
    await qdb.upsert_relations(items)
    point_ids = [row["point_id"] for row in rows]
    await asyncio.to_thread(manager.sqlite.mark_clean, "relations", point_ids)
    metrics.inc("failover_resync_uploaded", len(rows))
    stats["relations"] += len(rows)
    return True


async def resync_pending(manager: Optional[FailoverManager] = None) -> dict:
    """Upload dirty SQLite rows back to Qdrant in batches.

    Returns {"messages": n, "chats": n, "profiles": n, "relations": n, "errors": n}.
    """
    manager = manager or get_manager()
    stats = {"messages": 0, "chats": 0, "profiles": 0, "relations": 0, "errors": 0}

    if not manager.enabled:
        return stats
    if manager.resyncing:
        return stats
    if manager.mode != "qdrant":
        return stats

    manager.resyncing = True
    try:
        while manager.mode == "qdrant":
            progressed = False
            for table, handler in (
                ("messages", _resync_messages),
                ("chats", _resync_chats),
                ("profiles", _resync_profiles),
                ("relations", _resync_relations),
            ):
                try:
                    if await handler(manager, stats):
                        progressed = True
                except Exception as exc:  # noqa: BLE001
                    stats["errors"] += 1
                    manager.note_error(exc)
                    log(f"resync: {table} batch failed ({exc})")
                    if manager.mode != "qdrant":
                        return stats
            if not progressed:
                break
    finally:
        manager.resyncing = False

    if any(stats[t] for t in ("messages", "chats", "profiles", "relations")):
        log(
            "resync: uploaded "
            f"messages={stats['messages']} chats={stats['chats']} "
            f"profiles={stats['profiles']} relations={stats['relations']} "
            f"errors={stats['errors']}"
        )
    return stats


# ---------------------------------------------------------------------------
# 8. Health monitor loop
# ---------------------------------------------------------------------------
async def failover_monitor(manager: Optional[FailoverManager] = None) -> None:
    """Forever loop: probe Qdrant every config.FAILOVER_HEALTH_INTERVAL_SECONDS."""
    manager = manager or get_manager()
    if not manager.enabled:
        log("failover: disabled (FAILOVER_ENABLED=false); monitor not started")
        return

    interval = getattr(config, "FAILOVER_HEALTH_INTERVAL_SECONDS", 30.0)
    last_mode = manager.mode
    while True:
        try:
            await manager.probe_qdrant()
        except Exception as exc:  # noqa: BLE001
            log(f"failover: probe error ({exc})")

        if manager.mode != last_mode:
            log(f"failover: mode transition {last_mode} -> {manager.mode}")
            last_mode = manager.mode

        slept = 0.0
        while slept < interval:
            await asyncio.sleep(1.0)
            slept += 1.0


# ---------------------------------------------------------------------------
# 9. Entrypoint helpers
# ---------------------------------------------------------------------------
async def start_storage() -> None:
    """Called by main.py at startup. Never raises."""
    try:
        manager = get_manager()
        if not manager.enabled:
            log("storage: failover disabled; using Qdrant only")
            return

        try:
            await manager.probe_qdrant()
        except Exception as exc:  # noqa: BLE001
            log(f"storage: startup probe failed ({exc})")

        try:
            counts = manager.sqlite.counts()
        except Exception:  # noqa: BLE001
            counts = {}
        log(f"storage: startup mode={manager.mode} sqlite_counts={counts}")

        asyncio.create_task(failover_monitor(manager), name="failover-monitor")
    except Exception as exc:  # noqa: BLE001
        log(f"storage: start_storage error ({exc})")


async def stop_storage() -> None:
    """Flush dirty rows (best-effort) and close the SQLite connection. Never raises."""
    try:
        manager = get_manager()
        if manager.enabled and manager.mode == "sqlite":
            try:
                await asyncio.wait_for(resync_pending(manager), timeout=10.0)
            except Exception as exc:  # noqa: BLE001
                log(f"storage: final resync failed ({exc})")
        try:
            await asyncio.to_thread(manager.sqlite.close)
        except Exception as exc:  # noqa: BLE001
            log(f"storage: sqlite close failed ({exc})")
    except Exception as exc:  # noqa: BLE001
        log(f"storage: stop_storage error ({exc})")


def storage_status() -> dict:
    """JSON-safe convenience wrapper around get_manager().status()."""
    manager = get_manager()
    status = manager.status()
    status["note"] = "enabled" if manager.enabled else "disabled"
    return status
