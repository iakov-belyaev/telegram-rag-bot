"""Mock-driven verification of profiles.py (no network, canned scroll pages)."""
import asyncio
import sys
import pathlib as _pathlib
from types import SimpleNamespace

sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import config
import database as db_mod
import profiles
from datetime import datetime, timezone

FAIL = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")
    if not cond:
        FAIL.append(name)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---- canned message payloads (usernames >= 4 chars for the mention regex) ----
def msg_payload(sender_id, chat_id, text="", reply_to=None, date="2026-01-02 10:00:00+00:00",
                username="", name=""):
    return {
        "sender_id": sender_id, "chat_id": chat_id, "chat_title": f"Chat {chat_id}",
        "sender_username": username, "sender_name": name or f"User {sender_id}",
        "text": text, "date": date, "reply_to_user_id": reply_to,
    }


PAGE1 = [
    SimpleNamespace(payload=msg_payload(1, -100, "hello @bobby_dev how are you", username="alice_net", name="Alice")),
    SimpleNamespace(payload=msg_payload(2, -100, "doing great thanks", username="bobby_dev", name="Bob")),
    SimpleNamespace(payload=msg_payload(1, -100, "let us meet", reply_to=2, username="alice_net", name="Alice",
                                        date="2026-01-03 10:00:00+00:00")),
    SimpleNamespace(payload=msg_payload(1, -100, "third message from alice", username="alice_net", name="Alice",
                                        date="2026-01-04 10:00:00+00:00")),
]
PAGE2 = [
    SimpleNamespace(payload=msg_payload(2, -200, "second chat here", username="bobby_dev", name="Bob",
                                        date="2026-01-01 10:00:00+00:00")),
    SimpleNamespace(payload=msg_payload(2, -200, "another bob message", username="bobby_dev", name="Bob",
                                        date="2026-01-02 11:00:00+00:00")),
    SimpleNamespace(payload=msg_payload(2, -200, "and a third from bob", username="bobby_dev", name="Bob",
                                        date="2026-01-03 11:00:00+00:00")),
]


async def fake_scroll_page(offset):
    if offset is None:
        return PAGE1, "cursor1"
    return PAGE2, None


async def main():
    config.PROFILE_MIN_MESSAGES = 2  # canned users have 3 (alice) / 4 (bob) messages

    # ---- pure helpers ----
    pa = {"common_chat_ids": [-1, -2, -3], "common_chat_titles": ["A", "B", "C"]}
    pb = {"common_chat_ids": [-2, -3, -4], "common_chat_titles": ["B2", "C2", "D"]}
    common = profiles.compute_common_chats(pa, pb)
    check("common chats intersect", common["chat_ids"] == [-3, -2], extra=str(common))

    check("_parse_dt iso", profiles._parse_dt("2026-01-02 10:00:00+00:00") is not None)
    check("_parse_dt bad", profiles._parse_dt("not-a-date") is None)
    check("_pair sorted", profiles._pair(9, 4) == (4, 9))

    # ---- aggregation with canned pages ----
    profiles._scroll_page = fake_scroll_page
    stats = await profiles.aggregate_message_stats(profiles.FloodGate())
    u1, u2 = stats["users"][1], stats["users"][2]
    check("aggregation counts", u1["message_count"] == 3 and u2["message_count"] == 4,
          extra=f"u1={u1['message_count']} u2={u2['message_count']}")
    check("aggregation chats", set(u1["chats"].keys()) == {-100} and set(u2["chats"].keys()) == {-100, -200})
    check("aggregation reply edge", u1["reply_targets"].get(2, {}).get("count") == 1)
    check("aggregation mention resolution", u1["mentions"].get("bobby_dev", {}).get("target_id") == 2,
          extra=str(u1["mentions"]))
    check("aggregation username map", stats["username_map"].get("bobby_dev") == 2)
    check("aggregation date min", u2["first_seen"] == "2026-01-01 10:00:00+00:00", extra=u2["first_seen"])
    check("aggregation date max", u1["last_seen"] == "2026-01-04 10:00:00+00:00", extra=u1["last_seen"])
    check("chat_members", stats["chat_members"][-100] == {1: 3, 2: 1})

    # ---- relation assembly ----
    payloads = profiles.assemble_relation_payloads(stats)
    kinds = {(p["a_id"], p["b_id"], p["edge_kind"]) for p in payloads}
    check("relation co_member edge", (1, 2, "co_member") in kinds, extra=str(sorted(kinds)))
    check("relation replies edge", (1, 2, "replies") in kinds)
    check("relation mentions edge", (1, 2, "mentions") in kinds)
    replies_p = next(p for p in payloads if p["edge_kind"] == "replies")
    check("relation username populated", replies_p["a_username"] == "alice_net" and replies_p["b_username"] == "bobby_dev",
          extra=f"{replies_p['a_username']}/{replies_p['b_username']}")
    check("relation payload marker", all(p["kind"] == "relation" for p in payloads))

    # ---- build_profiles end-to-end (all externals mocked) ----
    original_enrich = profiles.enrich_user

    async def fake_get_profile(uid):
        return None

    async def fake_enrich(client, gate, uid, existing, context_chat_id=None, force_refresh=False):
        return {"bio": f"bio{uid}", "first_name": f"First{uid}", "last_name": "L",
                "username": "alice_net" if uid == 1 else "bobby_dev", "is_bot": False, "is_premium": False,
                "pfp_hash": "abcd" * 16, "pfp_meta": {"width": 100, "height": 100},
                "refreshed": True}

    upserts = {"profiles": 0, "relations": 0}

    async def fake_upsert_profiles(items):
        upserts["profiles"] += len(items)
        return len(items)

    async def fake_upsert_relations(items):
        upserts["relations"] += len(items)
        return len(items)

    async def fake_embed(texts):
        return [[0.5] * 384 for _ in texts]

    db_mod.get_profile = fake_get_profile
    profiles.enrich_user = fake_enrich
    db_mod.upsert_profiles = fake_upsert_profiles
    db_mod.upsert_relations = fake_upsert_relations
    db_mod.embed_texts = fake_embed

    result = await profiles.build_profiles(None, profiles.FloodGate(), profiles.RateGate(1000, 10))
    check("build_profiles no errors", result["errors"] == 0, extra=str(result))
    check("build_profiles qualifying 2", result["qualifying_users"] == 2, extra=str(result))
    check("build_profiles upserted 2", result["profiles_upserted"] == 2, extra=str(result))
    check("build_profiles relations upserted", result["relations_upserted"] >= 3, extra=str(result))
    check("upsert counts match", upserts["profiles"] == 2 and upserts["relations"] == result["relations_upserted"])

    # ---- enrich_user freshness branch (no network; restore real implementation first) ----
    profiles.enrich_user = original_enrich
    existing = {"updated_at": now_iso(), "bio": "cached bio", "pfp_hash": "cafe",
                "pfp_meta": {"width": 1}, "first_name": "F", "last_name": "L",
                "username": "u", "is_bot": False, "is_premium": True}
    info = await profiles.enrich_user(None, profiles.FloodGate(), 42, existing)
    check("enrich reuses fresh cache", info["refreshed"] is False and info["bio"] == "cached bio",
          extra=str(info))

    # stale cache must attempt network -> fake client to avoid real calls
    class FailingClient:
        async def get_users(self, uid):
            raise RuntimeError("network down")

    stale = dict(existing, updated_at="2000-01-01T00:00:00+00:00")
    info2 = await profiles.enrich_user(FailingClient(), profiles.FloodGate(), 42, stale)
    check("enrich survives network failure", info2["refreshed"] is False and info2["bio"] == "cached bio",
          extra=str(info2))

    # ---- introduce ----
    async def fake_get_profile2(uid):
        return {"user_id": uid, "username": f"u{uid}", "common_chat_ids": [-1, -2],
                "common_chat_titles": ["One", "Two"], "display_name": f"U{uid}"}

    async def fake_get_relations(uid, limit=50):
        return [{"a_id": 5, "b_id": 7, "edge_kind": "co_member", "weight": 3}]

    db_mod.get_profile = fake_get_profile2
    db_mod.get_relations_for = fake_get_relations
    intro = await profiles.introduce(7, 5)
    check("introduce common chats", intro["common_chats"]["chat_ids"] == [-2, -1])
    check("introduce finds relation", intro["relation"] is not None and intro["relation"]["weight"] == 3)

    print()
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)
    print("ALL PROFILES CHECKS PASSED")


asyncio.run(main())
