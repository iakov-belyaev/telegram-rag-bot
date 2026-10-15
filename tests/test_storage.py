"""Verification of storage.py: SQLite mirror, failover switch, recovery resync."""
import asyncio
import pathlib as _pathlib
import sys
import tempfile

sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import config

TMP = _pathlib.Path(tempfile.mkdtemp(prefix="storage_test_"))
config.STATE_DIR = TMP
config.FAILOVER_SQLITE_PATH = TMP / "failover.db"
config.FAILOVER_ENABLED = True
config.FAILOVER_DEGRADE_ERRORS = 3

from qdrant_client.models import PointStruct

import metrics
import storage

FAIL = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")
    if not cond:
        FAIL.append(name)


def pt(chat_id, mid, text, username="alice"):
    return PointStruct(
        id=storage.message_point_id(chat_id, mid),
        vector=[0.1] * 4,
        payload={"message_id": mid, "chat_id": chat_id, "chat_title": "Cyprus Biz",
                 "sender_id": 5, "sender_name": "Alice", "sender_username": username,
                 "text": text, "source_type": "text", "date": "2026-01-02 10:00:00+00:00",
                 "link": "https://t.me/c/1/2"},
    )


async def main():
    # ---------- SQLite mode ----------
    mgr = storage.FailoverManager(sqlite_path=config.FAILOVER_SQLITE_PATH, enabled=True)
    storage.reset_manager(mgr)
    mgr._use_sqlite("test")

    n = await storage.upsert_messages([pt(-100, 7, "hello watchlist phone +35799123456")])
    check("sqlite write count", n == 1)
    counts = mgr.sqlite.counts()
    check("sqlite counts + dirty", counts["messages"] == 1 and counts["dirty"] >= 1, extra=str(counts))

    hits = await storage.search_messages("hello", limit=5)
    check("sqlite search hit + payload", len(hits) == 1 and "hello" in (getattr(hits[0], "payload", {}) or {}).get("text", ""),
          extra=f"n={len(hits)}")
    check("sqlite chat filter", len(await storage.search_messages("hello", chat_id=-100)) == 1
          and len(await storage.search_messages("hello", chat_id=-200)) == 0)
    check("sqlite sender filter", len(await storage.search_messages("hello", sender_username="alice")) == 1
          and len(await storage.search_messages("hello", sender_username="bob")) == 0)

    existing = await storage.filter_existing_message_ids(-100, [7, 8])
    check("sqlite dedup guard", existing == {7}, extra=str(existing))

    await storage.upsert_profiles([(111, {"user_id": 111, "username": "alice", "display_name": "Alice",
                                          "bio": "dev", "message_count": 3}, [0.1] * 4)])
    prof = await storage.get_profile(111)
    check("sqlite profile roundtrip", prof is not None and prof["username"] == "alice")
    bulk = await storage.get_profiles_bulk([111, 222])
    check("sqlite bulk profiles", set(bulk.keys()) == {111})

    await storage.upsert_relations([({"a_id": 111, "b_id": 222, "edge_kind": "co_member", "weight": 3},
                                     [0.1] * 4)])
    rels = await storage.get_relations_for(222)
    check("sqlite relations", len(rels) == 1 and rels[0]["weight"] == 3)

    stats = await storage.collection_stats()
    check("sqlite stats shape + failover block",
          stats["telegram_messages"]["points"] == 1 and stats["failover"]["mode"] == "sqlite",
          extra=str(stats.get("failover")))
    check("storage_status()", storage.storage_status()["mode"] == "sqlite")

    # ---------- failover trigger ----------
    mgr2 = storage.FailoverManager(sqlite_path=TMP / "failover2.db", enabled=True)
    storage.reset_manager(mgr2)

    async def boom(*a, **kw):
        raise RuntimeError("qdrant down")

    real_upsert = storage.qdb.upsert_messages
    storage.qdb.upsert_messages = boom
    try:
        n1 = await storage.upsert_messages([pt(-100, 20, "first while down")])
        check("first error still writes to sqlite", n1 == 1 and mgr2.mode == "qdrant"
              and mgr2.consecutive_errors == 1, extra=mgr2.mode)
        await storage.upsert_messages([pt(-100, 21, "second while down")])
        await storage.upsert_messages([pt(-100, 22, "third while down")])
        check("degrades after threshold", mgr2.mode == "sqlite" and mgr2.degrade_events == 1,
              extra=f"mode={mgr2.mode} events={mgr2.degrade_events}")
        check("fallback writes landed", mgr2.sqlite.counts()["messages"] == 3, extra=str(mgr2.sqlite.counts()))
        check("failover metric", metrics.get_counter("failover_switches") >= 1)
        n4 = await storage.upsert_messages([pt(-100, 23, "fourth straight to sqlite")])
        check("sqlite mode skips qdrant entirely", n4 == 1 and mgr2.sqlite.counts()["messages"] == 4)
        hits2 = await storage.search_messages("while down", limit=10)
        check("reads served from sqlite while degraded", len(hits2) >= 3, extra=f"n={len(hits2)}")
    finally:
        storage.qdb.upsert_messages = real_upsert

    # ---------- recovery + resync ----------
    uploads = {"messages": [], "profiles": 0, "relations": 0}

    async def fake_embed(texts):
        return [[0.5] * 4 for _ in texts]

    async def fake_upsert_messages(points):
        uploads["messages"].extend(points)
        return len(points)

    async def fake_upsert_profiles(items):
        uploads["profiles"] += len(items)
        return len(items)

    async def fake_upsert_relations(items):
        uploads["relations"] += len(items)
        return len(items)

    real_embed = storage.qdb.embed_texts
    real_up = storage.qdb.upsert_messages
    real_upp = storage.qdb.upsert_profiles
    real_upr = storage.qdb.upsert_relations
    storage.qdb.embed_texts = fake_embed
    storage.qdb.upsert_messages = fake_upsert_messages
    storage.qdb.upsert_profiles = fake_upsert_profiles
    storage.qdb.upsert_relations = fake_upsert_relations
    try:
        # seed a profile + relation while degraded
        await storage.upsert_profiles([(999, {"user_id": 999, "username": "late", "display_name": "Late",
                                              "bio": "", "message_count": 1}, [0.1] * 4)])
        await storage.upsert_relations([({"a_id": 999, "b_id": 111, "edge_kind": "replies", "weight": 1},
                                         [0.1] * 4)])
        mgr2._use_qdrant()
        result = await storage.resync_pending(mgr2)
        check("resync uploaded messages", result["messages"] == 4 and len(uploads["messages"]) == 4,
              extra=str(result))
        check("resync uploaded profiles+relations", uploads["profiles"] == 1 and uploads["relations"] == 1)
        check("dirty cleared", mgr2.sqlite.counts()["dirty"] == 0, extra=str(mgr2.sqlite.counts()))
        check("resync metric", metrics.get_counter("failover_resync_uploaded") >= 5)
    finally:
        storage.qdb.embed_texts = real_embed
        storage.qdb.upsert_messages = real_up
        storage.qdb.upsert_profiles = real_upp
        storage.qdb.upsert_relations = real_upr

    # ---------- probes ----------
    async def ok_probe(fn, *a, **kw):
        from types import SimpleNamespace
        return SimpleNamespace(collections=[])

    real_call = storage.qdb._qdrant_call
    storage.qdb._qdrant_call = ok_probe
    try:
        check("probe healthy", await mgr2.probe_qdrant() is True)
    finally:
        storage.qdb._qdrant_call = real_call

    async def bad_probe(*a, **kw):
        raise RuntimeError("probe fail")

    storage.qdb._qdrant_call = bad_probe
    try:
        check("probe unhealthy", await mgr2.probe_qdrant() is False and mgr2.consecutive_errors >= 1)
    finally:
        storage.qdb._qdrant_call = real_call

    # ---------- re-exports for callers ----------
    for attr in ("embed_texts", "message_point_id", "build_message_payload", "build_profile_payload",
                 "build_relation_payload", "build_profile_embed_text", "build_relation_embed_text",
                 "get_qdrant", "_qdrant_read", "_qdrant_call"):
        check(f"re-export {attr}", hasattr(storage, attr))

    print()
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)
    print("ALL STORAGE CHECKS PASSED")


asyncio.run(main())
