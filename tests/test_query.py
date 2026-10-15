"""Verification of query.py: entity parsing, dossier fan-out, rendering."""
import asyncio
import json
import pathlib as _pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import query

FAIL = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")
    if not cond:
        FAIL.append(name)


def hit(payload, hid="h1"):
    return SimpleNamespace(payload=payload, id=hid, score=None)


def msgp(text, sender_id=1, username="alice", link="https://t.me/c/1/2", date="2026-01-02 10:00:00+00:00"):
    return {"text": text, "sender_id": sender_id, "sender_name": "Alice", "sender_username": username,
            "chat_id": -100, "chat_title": "Cyprus Biz", "message_id": 2, "link": link, "date": date}


async def main():
    # ---------- parse_entity ----------
    p = query.parse_entity("+357 99 123456")
    check("phone parsed", p["type"] == "phone" and p["digits"] == "35799123456" and "+35799123456" in p["variants"],
          extra=str(p))
    check("email parsed", query.parse_entity("someone@example.com")["type"] == "email")
    check("eth parsed", query.parse_entity("0x" + "a1" * 20)["type"] == "wallet")
    check("username parsed", query.parse_entity("@alice_net")["type"] == "username")
    check("user_id parsed", query.parse_entity("123456789")["type"] == "user_id")
    check("group parsed", query.parse_entity("https://t.me/cyprus_it")["type"] == "group")
    check("text fallback", query.parse_entity("some random phrase")["type"] == "text")

    # ---------- dossier: username ----------
    profile = {"user_id": 111, "username": "alice_net", "display_name": "Alice Net", "bio": "dev",
               "message_count": 12, "common_chat_ids": [-100], "common_chat_titles": ["Cyprus Biz"]}

    async def fake_search_messages(q, limit=10, chat_id=None, sender_username=None, **kw):
        return [hit(msgp(f"hello about {q}"))]

    async def fake_search_profiles(q, limit=10, **kw):
        return [hit(profile, hid="p1")]

    async def fake_get_relations(uid, limit=50):
        return [{"a_id": 111, "b_id": 222, "a_username": "alice_net", "b_username": "bob_dev",
                 "edge_kind": "co_member", "weight": 3}]

    async def fake_get_profile(uid):
        return profile if uid == 111 else None

    query.db.search_messages = fake_search_messages
    query.db.search_profiles = fake_search_profiles
    query.db.get_relations_for = fake_get_relations
    query.db.get_profile = fake_get_profile
    query.db.storage_status = lambda: {"mode": "sqlite"}

    res = await query.dossier("who is @alice_net")
    check("username dossier identity", res["identity"]["profile"] is profile, extra=str(res.get("error")))
    check("username dossier relations", len(res["relations"]) == 1)
    check("username dossier messages", res["stats"]["message_hits"] >= 1)
    check("backend reported", res["backend"] == "sqlite")
    check("timeline computed", res["timeline"]["first_seen"].startswith("2026-01-02"))

    # ---------- dossier: phone (bio match) ----------
    phone_profile = dict(profile, user_id=333, username="carol", bio="call me +357 99 123456 anytime")

    async def fake_search_profiles_phone(q, limit=10, **kw):
        return [hit(phone_profile, hid="p2")]

    query.db.search_profiles = fake_search_profiles_phone
    res2 = await query.dossier("give me everything on +35799123456")
    check("phone dossier bio match", any(m["user_id"] == 333 for m in res2["identity"]["bio_matches"]),
          extra=str(res2["identity"]))
    check("phone dossier messages hit", res2["stats"]["message_hits"] >= 1)

    # ---------- dossier: group ----------
    async def fake_find_chat(ref):
        return {"chat_id": -100, "chat_title": "Cyprus IT", "ref": ref, "message_hits": 2}

    query._find_chat = fake_find_chat
    res3 = await query.dossier("https://t.me/cyprus_it")
    check("group dossier chat", res3["chats"] and res3["chats"][0]["chat_id"] == -100, extra=str(res3["chats"])[:120])
    check("group dossier participants", len(res3["participants"]) >= 1)

    # ---------- error containment ----------
    async def exploding(q, limit=10, **kw):
        raise RuntimeError("db unavailable")

    query.db.search_messages = exploding
    res4 = await query.dossier("anything at all")
    check("errors contained (no raise, zero-stats dossier)",
          isinstance(res4, dict) and res4["stats"]["message_hits"] == 0 and "entity" in res4
          and "profile" in res4["identity"], extra=str(res4.get("error")))

    # ---------- render ----------
    check("render_text", "DOSSIER" in query.render_text(res) and isinstance(query.render_text(res), str))

    # ---------- CLI (json) in-process via _cli ----------
    query.db.search_messages = fake_search_messages
    rc = query._cli(["@alice_net", "--json", "--limit", "5"])
    check("cli exit 0", rc == 0)

    print()
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)
    print("ALL QUERY CHECKS PASSED")


asyncio.run(main())
