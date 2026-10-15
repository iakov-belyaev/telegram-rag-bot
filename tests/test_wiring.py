"""Wiring checks: main.py phase-2 integration (watchdog, watchlist job, imports)."""
import asyncio
import json
import pathlib as _pathlib
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import config

TMP = _pathlib.Path(tempfile.mkdtemp(prefix="wiring_test_"))
config.STATE_DIR = TMP
config.CONTROL_FLAGS_FILE = TMP / "control_flags.json"
config.CONTROL_FLAG_POLL_SECONDS = 0.05
config.WATCHLIST_FILE = TMP / "watchlist.json"
config.WATCHLIST_HITS_FILE = TMP / "watchlist_hits.jsonl"
config.WATCHLIST_FLUSH_SECONDS = 60

import control_cli
import main as main_mod
import storage
import watchlist

FAIL = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")
    if not cond:
        FAIL.append(name)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, uid, text, **kw):
        self.sent.append((uid, text))


class FakeControl:
    def __init__(self):
        self.stop_requested = False
        self.paused = False
        self.pause_reason = ""
        self.calls = []

    def pause(self, reason):
        self.paused = True
        self.pause_reason = reason
        self.calls.append(("pause", reason))

    def resume(self):
        self.paused = False
        self.pause_reason = ""
        self.calls.append(("resume",))

    def request_stop(self):
        self.calls.append(("stop",))


async def main():
    # ---- import surface ----
    for attr in ("_control_watchdog", "_watchlist_job", "storage_shield", "_make_sweep_fn"):
        check(f"main.{attr} present", hasattr(main_mod, attr))
    check("main.db is storage", main_mod.db is storage)
    check("main.db.start_storage", hasattr(main_mod.db, "start_storage"))
    check("main.db.stop_storage", hasattr(main_mod.db, "stop_storage"))

    # ---- control watchdog: pause flag -> control.pause ----
    control_cli._save({})  # clean flags
    control = FakeControl()
    watcher_task = asyncio.create_task(main_mod._control_watchdog(control))
    await asyncio.sleep(0.12)
    check("watchdog: idle -> no calls", control.calls == [], extra=str(control.calls))

    control_cli.set_flag("pause", True)
    await asyncio.sleep(0.25)
    check("watchdog: pause applied once", control.calls == [("pause", "flags")], extra=str(control.calls))

    control_cli.set_flag("pause", False)
    await asyncio.sleep(0.25)
    check("watchdog: resume applied", control.calls[-1] == ("resume",), extra=str(control.calls))

    control.stop_requested = True
    await asyncio.wait_for(watcher_task, timeout=5)
    check("watchdog exits on stop", True)
    control_cli._save({})

    # ---- watchlist job: queue -> admin DM ----
    watchlist.reset_watchlist()
    wl = watchlist.get_watchlist()
    await wl.add("+35799123456")
    hits = await watchlist.check_payload({
        "text": "call +35799123456 now", "chat_id": -100, "chat_title": "Cyprus Biz",
        "message_id": 9, "sender_name": "Alice", "sender_username": "alice",
        "link": "https://t.me/c/1/9",
    })
    check("hit queued", len(hits) == 1)

    bot = FakeBot()
    job = main_mod._watchlist_job(bot, 111)
    await job()
    check("job delivered hit", len(bot.sent) == 1 and "WATCHLIST HIT" in bot.sent[0][1],
          extra=str(bot.sent)[:120])
    await job()
    check("job drains queue (second run quiet)", len(bot.sent) == 1)

    print()
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)
    print("ALL WIRING CHECKS PASSED")


asyncio.run(main())
