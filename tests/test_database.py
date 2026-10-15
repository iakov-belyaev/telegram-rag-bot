"""Functional verification of database.py — pure builders + live embed + live read-only Qdrant."""
import asyncio
import sys
import pathlib as _pathlib
import uuid
from types import SimpleNamespace

sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import database as db

FAIL = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")
    if not cond:
        FAIL.append(name)


# ---------- Pure builder tests (no network) ----------
chat = SimpleNamespace(
    id=-1001234567890, title="Cyprus Business Network", username="cyprusbiz",
    first_name=None, type="ChatType.SUPERGROUP",
)
msg = SimpleNamespace(
    id=42,
    from_user=SimpleNamespace(id=999, first_name="Jane", last_name="Doe", username="janedoe"),
    date="2026-01-01 12:00:00",
)

payload = db.build_message_payload(chat, msg, "Hello world")
legacy_keys = {"message_id", "chat_id", "chat_title", "sender_id", "sender_name",
               "sender_username", "text", "source_type", "date", "link"}
check("message payload has legacy keys", legacy_keys.issubset(payload.keys()),
      extra=f"missing={legacy_keys - payload.keys()}")
check("link uses username", payload["link"] == "https://t.me/cyprusbiz/42")
check("sender parsed", payload["sender_id"] == 999 and payload["sender_username"] == "janedoe")

# Legacy-compatible point id (same formula as v1 core.py)
expected = str(uuid.uuid5(uuid.NAMESPACE_DNS, "-1001234567890_42"))
check("message_point_id legacy-compatible", db.message_point_id(-1001234567890, 42) == expected)

# Chat-as-sender fallback
msg2 = SimpleNamespace(id=43, from_user=None, date="2026-01-01 13:00:00")
p2 = db.build_message_payload(chat, msg2, "channel post")
check("chat-as-sender fallback", p2["sender_id"] == chat.id and p2["sender_username"] == "cyprusbiz")

# Profile payload + embed text
prof = db.build_profile_payload(
    user_id=999, username="janedoe", first_name="Jane", last_name="Doe",
    bio="Fintech consultant in Nicosia", pfp_hash="aabbcc",
    common_chat_ids=[-1001, -1002], common_chat_titles=["Cyprus Business", "Nicosia Startups"],
    message_count=57, interactions={"111": 4, "222": 1},
)
check("profile payload kind", prof.get("kind") == "profile")
check("profile sorted chats", prof["common_chat_ids"] == [-1002, -1001])
petext = db.build_profile_embed_text(prof)
check("profile embed text non-empty", len(petext) > 40, extra=f"-> {petext[:80]}...")

# Relation payload sorted ids
rel = db.build_relation_payload(222, "userb", 111, "usera", "co_member", 3, [-1001], "2026-01-01", "2026-02-01")
check("relation ids sorted", rel["a_id"] == 111 and rel["b_id"] == 222 and rel["a_username"] == "usera")
check("relation edge_kind kept", rel["edge_kind"] == "co_member")
retxt = db.build_relation_embed_text(rel)
check("relation embed text non-empty", len(retxt) > 20)

# Relation point id determinism
check("relation_point_id order-insensitive",
      db.relation_point_id(1, 2, "co_member") == db.relation_point_id(2, 1, "co_member"))


# ---------- Live tests ----------
async def live():
    embs = await db.embed_texts(["hello world", "cyprus networking"])
    check("embed_texts returns 2x384", len(embs) == 2 and len(embs[0]) == 384,
          extra=f"dims={len(embs[0])}")
    check("embed floats", all(isinstance(x, float) for x in embs[0][:5]))

    stats = await db.collection_stats()
    print("collection_stats:", stats)
    check("stats keyed by collection names", "telegram_messages" in stats and "telegram_profiles" in stats)
    check("messages points > 0", (stats["telegram_messages"]["points"] or 0) > 0)

    # Dedup guard against real data: pull 3 real message ids from cluster, check they're detected as existing
    from qdrant_client import models as qm
    qd = db.get_qdrant()
    points, _ = qd.scroll(collection_name="telegram_messages", limit=3,
                          with_payload=["chat_id", "message_id"], with_vectors=False)
    probe_chat = points[0].payload["chat_id"]
    probe_ids = [p.payload["message_id"] for p in points if p.payload["chat_id"] == probe_chat]
    found = await db.filter_existing_message_ids(probe_chat, probe_ids + [10**12])
    check("dedup guard finds existing real ids", set(probe_ids).issubset(found))
    check("dedup guard excludes fake id", 10**12 not in found)

    # search_messages live
    hits = await db.search_messages("business", limit=3)
    check("search_messages live returns hits", len(hits) > 0, extra=f"top_score={hits[0].score:.3f}" if hits else "")

    # get_profile for a non-existent user -> None
    none_prof = await db.get_profile(123456789012345678)
    check("get_profile missing -> None", none_prof is None)


asyncio.run(live())

print()
if FAIL:
    print("FAILURES:", FAIL)
    sys.exit(1)
print("ALL CHECKS PASSED")
