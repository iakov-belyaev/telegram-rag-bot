"""Verification of watchlist.py: matching, persistence, hits queue, drain."""
import asyncio
import json
import pathlib as _pathlib
import sys
import tempfile

sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import config

TMP = _pathlib.Path(tempfile.mkdtemp(prefix="watchlist_test_"))
config.STATE_DIR = TMP
config.WATCHLIST_FILE = TMP / "watchlist.json"
config.WATCHLIST_HITS_FILE = TMP / "watchlist_hits.jsonl"

import metrics
import watchlist

FAIL = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")
    if not cond:
        FAIL.append(name)


async def main():
    wl = watchlist.Watchlist()
    check("empty watchlist", wl.tokens() == [])
    check("add token", await wl.add("+35799123456", note="suspect") is True)
    check("dedupe token", await wl.add("+35799123456") is False)
    check("add username", await wl.add("@scam_master") is True)

    wl2 = watchlist.Watchlist()
    check("persistence", sorted(wl2.tokens()) == ["+35799123456", "@scam_master"])
    watchlist.reset_watchlist()  # ensure singleton rebuilds for check_payload

    # ---- pure matching ----
    tokens = ["+35799123456", "@scam_master"]
    hit = watchlist.match_message({"text": "call +357 99 123 456 now"}, tokens)
    check("phone digit-variant match", hit == ["+35799123456"], extra=str(hit))
    hit2 = watchlist.match_message({"text": "DM Scam_Master for deals"}, tokens)
    check("username case-insensitive", hit2 == ["@scam_master"], extra=str(hit2))
    check("no match", watchlist.match_message({"text": "weather is nice"}, tokens) == [])
    short = watchlist.match_message({"text": "id 12345 here"}, ["12345"])
    check("short numeric not digit-matched", short == [])

    # ---- check_payload + hits queue ----
    before = metrics.get_counter("watchlist_hits")
    hits = await watchlist.check_payload({
        "text": "contact +35799123456", "chat_id": -100, "chat_title": "Cyprus Biz",
        "message_id": 7, "sender_name": "Alice", "sender_username": "alice",
        "link": "https://t.me/c/1/7",
    })
    check("check_payload hit", len(hits) == 1 and hits[0]["token"] == "+35799123456", extra=str(hits))
    check("metric bumped", metrics.get_counter("watchlist_hits") == before + 1)
    check("hits file written", config.WATCHLIST_HITS_FILE.exists()
          and len(config.WATCHLIST_HITS_FILE.read_text().strip().splitlines()) == 1)

    # ---- drain ----
    sent = []

    async def send_fn(text):
        sent.append(text)

    n = await watchlist.drain_hits(send_fn)
    check("drain delivers", n == 1 and "WATCHLIST HIT" in sent[0], extra=str(sent))
    check("drain clears queue", config.WATCHLIST_HITS_FILE.read_text().strip() == "")
    check("drain empty second time", await watchlist.drain_hits(send_fn) == 0)

    # ---- remove ----
    check("remove", await wl.remove("@scam_master") is True)
    check("remove missing", await wl.remove("@scam_master") is False)

    print()
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)
    print("ALL WATCHLIST CHECKS PASSED")


asyncio.run(main())
