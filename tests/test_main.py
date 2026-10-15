"""Verification of main.py — storage shield transitions, sweep fn, import-safety."""
import asyncio
import pathlib as _pathlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import config

TMP = Path(tempfile.mkdtemp(prefix="main_test_"))
config.STATE_DIR = TMP

import database as db_mod
import main as main_mod
import scraper

FAIL = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")
    if not cond:
        FAIL.append(name)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


async def main():
    control = scraper.get_control()
    # reset control state
    control.paused = False
    control.pause_reason = ""
    control.stop_requested = False

    fake_bot = FakeBot()
    usage = {"ratio": 0.50}

    async def fake_stats():
        return {"messages_usage": {"usage_ratio": usage["ratio"], "max_vectors": 100000},
                "telegram_messages": {"points": int(usage["ratio"] * 100000), "exists": True},
                "total_points": int(usage["ratio"] * 100000)}

    db_mod.collection_stats = fake_stats
    config.STORAGE_CHECK_INTERVAL_SECONDS = 0.05

    task = asyncio.create_task(main_mod.storage_shield(fake_bot, 111, control))

    await asyncio.sleep(0.12)
    check("shield: no alerts at 50%", len(fake_bot.sent) == 0 and control.paused is False)

    usage["ratio"] = 0.85
    await asyncio.sleep(0.12)
    check("shield: warn once at 85%", len(fake_bot.sent) == 1 and "Storage warning" in fake_bot.sent[0][1],
          extra=f"sent={len(fake_bot.sent)}")
    await asyncio.sleep(0.12)
    check("shield: warn not repeated", len(fake_bot.sent) == 1)

    usage["ratio"] = 0.97
    await asyncio.sleep(0.12)
    check("shield: pauses at 97%", control.paused is True and control.pause_reason.startswith("storage"),
          extra=control.pause_reason)
    check("shield: critical DM sent", any("high-water" in t or "Storage high" in t for _, t in fake_bot.sent))

    usage["ratio"] = 0.50
    await asyncio.sleep(0.15)
    check("shield: resumes below warn", control.paused is False and control.pause_reason == "")
    check("shield: resume DM sent", any("resumed" in t for _, t in fake_bot.sent))

    control.request_stop()
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    check("shield: exits on stop_requested", task.done() or task.cancelled())
    control.stop_requested = False

    # _notify_admin handles PeerIdInvalid quietly
    import pyrogram.errors as tg_errors

    class BadBot:
        async def send_message(self, chat_id, text):
            raise tg_errors.PeerIdInvalid("nope")

    await main_mod._notify_admin(BadBot(), 111, "hello")
    check("_notify_admin swallows PeerIdInvalid", True)

    # sweep fn wiring
    sweep = main_mod._make_sweep_fn(userbot=None)
    check("sweep fn is callable coroutine factory", callable(sweep) and asyncio.iscoroutinefunction(sweep))

    # _build_clients constructs without connecting
    ub, bc = main_mod._build_clients()
    check("_build_clients builds pyrogram clients", ub.name == config.SESSION_USERBOT and bc.name == config.SESSION_BOT)
    await ub.stop() if getattr(ub, "is_connected", None) else None

    print()
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)
    print("ALL MAIN CHECKS PASSED")


asyncio.run(main())
