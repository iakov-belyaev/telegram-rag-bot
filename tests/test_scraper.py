"""Mock-driven verification of scraper.py machinery (no Telegram network)."""
import asyncio
import pathlib as _pathlib
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import config

# Redirect state files to a scratch dir BEFORE touching stores
TMP = Path(tempfile.mkdtemp(prefix="scraper_test_"))
config.STATE_DIR = TMP
config.SCRAPER_STATE_FILE = TMP / "scraper_state.json"
config.CHAT_FILTER_CACHE_FILE = TMP / "chat_filter_cache.json"
config.TARGET_GROUPS_FILE = TMP / "target_groups.json"

from pyrogram import errors as tg_errors  # noqa: E402

import database as db_mod  # noqa: E402
import scraper  # noqa: E402

FAIL = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")
    if not cond:
        FAIL.append(name)


def make_chat(cid, title="Test Group", username=None):
    return SimpleNamespace(id=cid, title=title, username=username, type="ChatType.SUPERGROUP")


async def main():
    # ---- FloodGate ----
    gate = scraper.FloodGate()
    check("gate initially clear", gate.remaining == 0.0)
    t0 = time.monotonic()
    await gate.trip(0.3)
    r = gate.remaining
    check("gate trips with margin", 0.3 * config.FLOOD_WAIT_MARGIN <= r <= 3.5, extra=f"remaining={r:.2f}")
    await gate.wait()
    elapsed = time.monotonic() - t0
    check("gate.wait blocks until clear", elapsed >= 0.3, extra=f"elapsed={elapsed:.2f}s")
    check("flood_events counted", gate.flood_events == 1)

    # ---- telegram_call FloodWait retry ----
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise tg_errors.FloodWait(0)  # value=0 -> gate trips with ~jitter only
        return "ok"

    res = await scraper.telegram_call(gate, flaky)
    check("telegram_call retries through FloodWait", res == "ok" and calls["n"] == 3)

    async def always_flood():
        raise tg_errors.FloodWait(0)

    raised = False
    try:
        # shrink margin to keep test fast
        config.FLOOD_WAIT_MARGIN = 0.05
        await scraper.telegram_call(gate, always_flood)
    except tg_errors.FloodWait:
        raised = True
    check("telegram_call re-raises after retries", raised)

    # ---- RateGate ----
    rg = scraper.RateGate(rate_per_second=20.0, burst=1)
    t0 = time.monotonic()
    for _ in range(3):
        await rg.acquire()
    el = time.monotonic() - t0
    check("rate gate paces (3 @ 20/s burst1)", el >= 0.08, extra=f"{el:.3f}s")

    # ---- CursorStore ----
    cs = scraper.CursorStore()
    cs.load()
    check("cursor empty default", cs.get(12345) is None)
    await cs.update(12345, title="G", offset_id=5, messages_indexed=7)
    e = cs.get(12345)
    check("cursor update/get roundtrip", e["offset_id"] == 5 and e["messages_indexed"] == 7 and "updated_at" in e)
    check("cursor persisted to disk", config.SCRAPER_STATE_FILE.exists())
    cs2 = scraper.CursorStore(); cs2.load()
    check("cursor reload from disk", (cs2.get(12345) or {}).get("offset_id") == 5)

    # ---- ChatFilterCache ----
    fc = scraper.ChatFilterCache(); fc.load()
    check("filter cache miss", fc.get(1) is None)
    await fc.set(1, False, "noise-title:meme")
    check("filter cache hit", fc.get(1) == (False, "noise-title:meme"))
    # TTL expiry
    config.CHAT_FILTER_CACHE_TTL_SECONDS = -1
    check("filter cache ttl expiry", fc.get(1) is None)
    config.CHAT_FILTER_CACHE_TTL_SECONDS = 86400

    # ---- TargetManager + join_converge ----
    tm = scraper.TargetManager(); tm.load()
    check("target add", await tm.add("@cyprus_biz") is True)
    check("target dedupe", await tm.add("@cyprus_biz") is False)
    check("target summary", tm.summary()["pending"] == 1 and tm.summary()["total"] == 1)

    class FakeClient:
        def __init__(self):
            self.joined = []

        async def join_chat(self, peer):
            if peer == "@fails":
                raise tg_errors.UserAlreadyParticipant(peer)
            self.joined.append(peer)
            return SimpleNamespace(id=-1, title="Joined " + peer)

        async def get_chat(self, peer):
            return SimpleNamespace(title="Joined " + peer)

    fake = FakeClient()
    await tm.add("@fails")
    jr = await scraper.join_converge(fake, gate, rg, tm)
    check("join_converge handles success+already-participant", jr == {"joined": 2, "error": 0}, extra=str(jr))
    states = {t["peer"]: t["state"] for t in tm.all()}
    check("target states joined", states.get("@cyprus_biz") == "joined" and states.get("@fails") == "joined")

    # ---- is_high_value_chat ----
    cache = scraper.ChatFilterCache()

    async def empty_history(chat_id, limit=30):
        return
        yield  # pragma: no cover

    class FilterClient:
        async def get_chat_history(self, chat_id, limit=30, offset_id=0):
            for i in range(15):
                yield SimpleNamespace(id=i, text="hello there friends", caption=None)

    noise_chat = make_chat(1, title="Best Memes Cyprus", username="memescy")
    verdict, reason = await scraper.is_high_value_chat(None, gate, cache, noise_chat)
    check("noise chat rejected", verdict is False and "noise-title" in reason, extra=reason)
    verdict2, _ = await scraper.is_high_value_chat(None, gate, cache, noise_chat)
    check("noise verdict cached", verdict2 is False)

    good_chat = make_chat(2, title="Cyprus Business Network", username="cybiz")
    verdict3, reason3 = await scraper.is_high_value_chat(FilterClient(), gate, cache, good_chat)
    check("clean chat accepted", verdict3 is True and reason3 == "ok", extra=f"{verdict3} {reason3}")

    # ---- index_messages_batch (mocked db layer) ----
    calls_log = {"embedded": 0, "upserted": 0, "exists_checked": None}

    async def fake_filter_existing(chat_id, ids):
        calls_log["exists_checked"] = list(ids)
        return {ids[0]}  # pretend first id already indexed

    async def fake_embed(texts):
        calls_log["embedded"] += len(list(texts))
        return [[0.1] * 384 for _ in texts]

    async def fake_upsert_messages(points):
        calls_log["upserted"] += len(points)
        return len(points)

    async def fake_upsert_chat(chat):
        return None

    db_mod.filter_existing_message_ids = fake_filter_existing
    db_mod.embed_texts = fake_embed
    db_mod.upsert_messages = fake_upsert_messages
    db_mod.upsert_chat = fake_upsert_chat

    msgs = [
        SimpleNamespace(id=1, text="first message here", caption=None, photo=None, document=None, date="d", image=None),
        SimpleNamespace(id=2, text="second message here", caption=None, photo=None, document=None, date="d", image=None),
        SimpleNamespace(id=3, text="", caption="", photo=None, document=None, date="d", image=None),
    ]
    n = await scraper.index_messages_batch(None, gate, good_chat, msgs)
    check("batch dedup skips existing", calls_log["exists_checked"] == [1, 2], extra=str(calls_log["exists_checked"]))
    check("batch indexes only new+nonempty", n == 1 and calls_log["upserted"] == 1, extra=f"n={n}")
    check("counters updated", scraper.get_control().counters["messages_indexed"] == 1)

    # ---- sync_chat_history pagination (mocked) ----
    pages = {
        0: [SimpleNamespace(id=10 + i, text=f"msg page1 {i}", caption=None, photo=None, document=None, date="d") for i in range(2)],
        12: [],  # offset after page 1 ids 10,11 -> offset_id becomes 11 -> next call returns []
    }

    class PageClient:
        async def get_chat_history(self, chat_id, limit=100, offset_id=0):
            if offset_id == 0:
                for m in pages[0]:
                    yield m
            # else empty

    await asyncio.sleep(0)
    config.SCRAPER_PAGE_SIZE = 2
    config.SCRAPER_MIN_DELAY_SECONDS = 0.01
    db_mod.filter_existing_message_ids = lambda cid, ids: asyncio.sleep(0, result=set())
    cursors = scraper.CursorStore()
    control = scraper.get_control()
    total = await scraper.sync_chat_history(PageClient(), gate, rg, cursors, good_chat, control)
    entry = cursors.get(good_chat.id) or {}
    check("pagination indexed page", total == 2, extra=f"total={total}")
    check("pagination marked completed", entry.get("completed") is True)

    print()
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)
    print("ALL SCRAPER CHECKS PASSED")


asyncio.run(main())
