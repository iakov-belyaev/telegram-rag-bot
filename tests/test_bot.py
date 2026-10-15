"""Mock-driven verification of bot.py — intelligence + auth flow + admin panel + inline."""
import asyncio
import pathlib as _pathlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import bot
import config
import database as db_mod
import profiles as profiles_mod
import rbac
import scraper

FAIL = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")
    if not cond:
        FAIL.append(name)


ADMIN_ID = 999001


class Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, text, **kwargs):
        self.calls.append(text)


class FakeClient:
    """Captures pyrogram handler decorators."""

    def __init__(self):
        self.message_handlers = []
        self.inline_handlers = []
        self.callback_handlers = []

    def on_message(self, filters=None):
        def deco(fn):
            self.message_handlers.append(fn)
            return fn

        return deco

    def on_inline_query(self):
        def deco(fn):
            self.inline_handlers.append(fn)
            return fn

        return deco

    def on_callback_query(self, filters=None):
        def deco(fn):
            self.callback_handlers.append(fn)
            return fn

        return deco


def find_handler(handlers, name):
    for fn in handlers:
        if getattr(fn, "__name__", "") == name:
            return fn
    raise KeyError(name)


async def main():
    # ---- monkeypatch externals ----
    async def echo_llm(system, user, max_tokens=None):
        return "LLM-ECHO: " + user

    bot._deepseek_chat = echo_llm

    async def fake_search_messages(query, limit=10, **kw):
        return [SimpleNamespace(payload={
            "sender_name": "Alice Net", "sender_username": "alice_net",
            "chat_title": "Cyprus Biz", "text": f"about {query}"})]

    async def fake_search_profiles(query, limit=5):
        return [SimpleNamespace(payload={
            "display_name": "Bobby Dev", "username": "bobby_dev",
            "bio": "builder", "common_chat_ids": [-1, -2]})]

    db_mod.search_messages = fake_search_messages
    db_mod.search_profiles = fake_search_profiles

    async def fake_whois(uid):
        return {
            "profile": {
                "user_id": uid, "display_name": "Alice Net", "username": "alice_net",
                "message_count": 57, "common_chat_ids": [-1, -2, -3],
                "common_chat_titles": ["Cyprus Biz", "Nicosia Startups", "Limassol"],
                "interactions": {"123": 5, "456": 2},
                "first_seen": "2026-01-01 10:00:00+00:00", "last_seen": "2026-03-01 10:00:00+00:00",
                "bio": "fintech consultant", "pfp_hash": "abcd" * 16, "pfp_meta": {"width": 100},
            },
            "relations": [
                {"a_id": 1, "b_id": 2, "a_username": "alice_net", "b_username": "bob_dev",
                 "edge_kind": "co_member", "weight": 3},
            ],
        }

    profiles_mod.whois = fake_whois

    async def fake_bulk(ids):
        return {123: {"display_name": "Known Alice", "username": "alice_net"}}

    db_mod.get_profiles_bulk = fake_bulk

    async def fake_introduce(a, b):
        return {
            "a": {"display_name": "Alice Net", "username": "alice_net", "message_count": 57,
                  "common_chat_ids": [-1, -2]},
            "b": {"display_name": "Bobby Dev", "username": "bobby_dev", "message_count": 20,
                  "common_chat_ids": [-2, -3]},
            "common_chats": {"chat_ids": [-2], "titles": ["Nicosia Startups"]},
            "relation": {"edge_kind": "co_member", "weight": 1},
        }

    profiles_mod.introduce = fake_introduce

    async def fake_collection_stats():
        return {
            "telegram_messages": {"points": 100, "exists": True},
            "telegram_chats": {"points": 5, "exists": True},
            "telegram_profiles": {"points": 10, "exists": True},
            "telegram_relations": {"points": 20, "exists": True},
            "messages_usage": {"max_vectors": 100000, "usage_ratio": 0.38},
            "total_points": 135,
        }

    db_mod.collection_stats = fake_collection_stats

    # ---- unit: helpers ----
    handles = bot._extract_handles(f"hello @alice_net and @bobby_dev {config.BOT_USERNAME}")
    check("_extract_handles excludes bot", handles == ["alice_net", "bobby_dev"], extra=str(handles))
    check("_classify whois", bot._classify("who is @alice_net")[0] == "whois")
    check("_classify introduce", bot._classify("introduce @alice_net @bobby_dev")[0] == "introduce")
    check("_classify search", bot._classify("what about server costs")[0] == "search")
    check("_truncate", bot._truncate("x" * 50, 10).endswith("..."))

    # ---- intelligence functions ----
    casual = await bot.rag_answer("server setup", deep=False)
    check("rag_answer casual uses payload", "Alice Net" in casual and "LLM-ECHO" in casual, extra=casual[:60])
    deep = await bot.rag_answer("server setup", deep=True)
    check("rag_answer deep includes profiles", "MATCHING PROFILES" in deep and "Bobby Dev" in deep)

    cw = await bot.casual_whois("@alice_net", 111)
    check("casual_whois counts groups", "Active in 3 groups" in cw and "Indexed messages: 57" in cw, extra=cw[:80].replace("\n", "|"))

    dw = await bot.deep_whois(111)
    check("deep_whois groups block", "Nicosia Startups" in dw)
    check("deep_whois interactions resolved", "Known Alice (5)" in dw, extra=dw[dw.find("interactions"):][:120].replace("\n", "|"))
    check("deep_whois relations edge_kind", "co_member: @alice_net <-> @bob_dev (3)" in dw)

    ci = await bot.casual_introduce(1, 2, "@alice_net", "@bobby_dev")
    check("casual_introduce counts chat_ids", "1 shared communities" in ci, extra=ci)
    di = await bot.deep_introduce(1, 2)
    check("deep_introduce titles", "Nicosia Startups" in di and "co_member" in di)

    # ---- handle_nlp gating ----
    async def fake_resolve(handle):
        return 111 if handle == "alice_net" else None

    bot._resolve_handle = fake_resolve

    rec = Recorder()
    msg = SimpleNamespace(from_user=SimpleNamespace(id=5), reply_text=rec, chat=SimpleNamespace(type="private"))
    await bot.handle_nlp(None, msg, "who is @alice_net", deep_allowed=False)
    check("group whois is casual + locked hint", "Active in 3 groups" in rec.calls[-1] and "🔒" in rec.calls[-1])

    rec2 = Recorder()
    msg2 = SimpleNamespace(from_user=SimpleNamespace(id=ADMIN_ID), reply_text=rec2, chat=SimpleNamespace(type="private"))
    await bot.handle_nlp(None, msg2, "who is @alice_net", deep_allowed=True)
    check("admin whois is deep", "DEEP DOSSIER" in rec2.calls[-1])

    # ---- handlers: registration + auth flow + panel ----
    tmp = Path(tempfile.mkdtemp(prefix="bot_test_"))
    config.TARGET_GROUPS_FILE = tmp / "target_groups.json"
    config.STATE_DIR = tmp

    fc = FakeClient()
    bot.register_handlers(fc, ADMIN_ID)
    check("handlers registered", len(fc.message_handlers) >= 13 and len(fc.inline_handlers) == 1 and len(fc.callback_handlers) == 1,
          extra=f"msg={len(fc.message_handlers)} inline={len(fc.inline_handlers)} cb={len(fc.callback_handlers)}")

    auth_h = find_handler(fc.message_handlers, "_cmd_auth")
    text_h = find_handler(fc.message_handlers, "_private_text")
    status_h = find_handler(fc.message_handlers, "_cmd_status")
    join_h = find_handler(fc.message_handlers, "_cmd_join")
    cb_h = find_handler(fc.callback_handlers, "_panel_callback")
    inline_h = fc.inline_handlers[0]

    # non-admin /auth
    r = Recorder()
    await auth_h(None, SimpleNamespace(from_user=SimpleNamespace(id=5), text="/auth", reply_text=r))
    check("non-admin /auth rejected", "Not authorized" in r.calls[-1])

    # admin /auth issues challenge
    r = Recorder()
    await auth_h(None, SimpleNamespace(from_user=SimpleNamespace(id=ADMIN_ID), text="/auth", reply_text=r))
    check("admin /auth issues nonce", "Nonce:" in r.calls[-1] and "rbac.py respond" in r.calls[-1])
    nonce = rbac.get_auth()._challenges[ADMIN_ID]["nonce"]
    good = rbac.get_auth().response_for_nonce(nonce)

    # wrong code first
    r = Recorder()
    await text_h(None, SimpleNamespace(from_user=SimpleNamespace(id=ADMIN_ID), text="0000000000000000", reply_text=r))
    check("wrong code hint", "Not authenticated" in r.calls[-1] or "not authenticated" in r.calls[-1])

    # correct code
    r = Recorder()
    await text_h(None, SimpleNamespace(from_user=SimpleNamespace(id=ADMIN_ID), text=good, reply_text=r))
    check("correct code authenticates", "Authenticated" in r.calls[-1] and rbac.get_auth().is_authenticated(ADMIN_ID), extra=r.calls[-1][:60])

    # /status now allowed
    r = Recorder()
    await status_h(None, SimpleNamespace(from_user=SimpleNamespace(id=ADMIN_ID), text="/status", reply_text=r))
    check("/status works when authenticated", "Scraper status" in r.calls[-1] and "last sweep" in r.calls[-1])

    # /join queues target
    r = Recorder()
    await join_h(None, SimpleNamespace(from_user=SimpleNamespace(id=ADMIN_ID), text="/join @cyprus_networking", reply_text=r))
    check("/join queues target", "queued" in r.calls[-1], extra=r.calls[-1])

    # callback pause + edit
    edits = Recorder()
    answers = Recorder()
    cq = SimpleNamespace(from_user=SimpleNamespace(id=ADMIN_ID), data="p:pause",
                         message=SimpleNamespace(edit_text=edits), answer=answers)
    await cb_h(None, cq)
    check("callback pauses scraper", scraper.get_control().paused is True and "Paused" in answers.calls[-1])
    cq2 = SimpleNamespace(from_user=SimpleNamespace(id=ADMIN_ID), data="p:resume",
                          message=SimpleNamespace(edit_text=edits), answer=answers)
    await cb_h(None, cq2)
    check("callback resumes + edits panel", scraper.get_control().paused is False and "Control panel" in edits.calls[-1])

    # callback from non-admin
    answers2 = Recorder()
    cq3 = SimpleNamespace(from_user=SimpleNamespace(id=5), data="p:pause",
                          message=SimpleNamespace(edit_text=edits), answer=answers2)
    await cb_h(None, cq3)
    check("callback rejects non-admin", "Not authenticated" in answers2.calls[-1])

    # inline query (admin -> deep cards)
    iq_answers = Recorder()
    iq = SimpleNamespace(from_user=SimpleNamespace(id=ADMIN_ID), query="who is @alice_net", answer=iq_answers)
    await inline_h(None, iq)
    results = iq_answers.calls[-1]
    check("inline returns results", isinstance(results, list) and len(results) >= 2, extra=f"n={len(results)}")

    # panel_snapshot standalone
    snap = await bot.panel_snapshot()
    check("panel_snapshot includes db", "DB total points" in snap and "Targets:" in snap)

    print()
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)
    print("ALL BOT CHECKS PASSED")


asyncio.run(main())
