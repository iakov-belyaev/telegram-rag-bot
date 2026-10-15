"""Verification of export.py: dossier exports in json/jsonl/csv/html."""
import asyncio
import json
import pathlib as _pathlib
import sys
import tempfile

sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import config

TMP = _pathlib.Path(tempfile.mkdtemp(prefix="export_test_"))
config.STATE_DIR = TMP
config.EXPORT_DIR = TMP / "exports"

import export
import query

FAIL = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")
    if not cond:
        FAIL.append(name)


CANNED = {
    "query": "@alice_net", "entity": {"type": "username", "value": "alice_net", "raw": "@alice_net"},
    "identity": {"profile": {"user_id": 1, "username": "alice_net", "display_name": "Alice",
                             "bio": "dev", "message_count": 5, "first_seen": "a", "last_seen": "b"},
                 "bio_matches": [], "ids": [1], "aliases": []},
    "profiles": [{"user_id": 1, "username": "alice_net", "display_name": "Alice"}],
    "relations": [], "chats": [{"chat_id": -100, "chat_title": "Cyprus Biz", "count": 3}],
    "messages": [{"date": "2026-01-02", "chat_title": "Cyprus Biz", "chat_id": -100,
                  "sender_name": "Alice", "sender_username": "alice_net",
                  "link": "https://t.me/c/1/2", "text": "hello world & <friends>"}],
    "context_messages": [], "participants": [],
    "timeline": {"first_seen": "2026-01-02", "last_seen": "2026-01-02"},
    "stats": {"message_hits": 1, "profile_hits": 1, "relation_hits": 0, "chat_hits": 1},
    "generated_at": "2026-01-02T10:00:00+00:00", "backend": "sqlite",
}


async def main():
    async def fake_dossier(q, limit=None):
        return CANNED

    query.dossier = fake_dossier

    res = await export.export_dossier("@alice_net", fmt="all")
    written = res["written"]
    check("4 files written", len(written) == 4, extra=str(written))
    for p in written:
        check(f"exists {_pathlib.Path(p).suffix}", _pathlib.Path(p).exists())

    html_text = next(p for p in written if p.endswith(".html"))
    html_content = _pathlib.Path(html_text).read_text(encoding="utf-8")
    check("html escaped + rendered", "Dossier: @alice_net" in html_content
          and "&lt;friends&gt;" in html_content and "&amp;" in html_content)

    csv_text = next(p for p in written if p.endswith(".csv"))
    rows = _pathlib.Path(csv_text).read_text(encoding="utf-8").strip().splitlines()
    check("csv header+row", rows[0].startswith("date,chat_title") and len(rows) == 2, extra=str(rows[:2])[:150])

    json_text = next(p for p in written if p.endswith(".json"))
    data = json.loads(_pathlib.Path(json_text).read_text(encoding="utf-8"))
    check("json roundtrip", data["query"] == "@alice_net" and data["stats"]["message_hits"] == 1)

    jsonl_text = next(p for p in written if p.endswith(".jsonl"))
    lines = _pathlib.Path(jsonl_text).read_text(encoding="utf-8").strip().splitlines()
    check("jsonl lines", len(lines) == 2 and json.loads(lines[1])["type"] == "message")

    # CLI — run in a worker thread: _cli() calls asyncio.run() internally and
    # cannot execute inside this file's running event loop.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        rc = ex.submit(
            export._cli, ["@alice_net", "--format", "json", "--out", str(TMP / "cli_out")]
        ).result()
    check("cli exit 0", rc == 0)

    print()
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)
    print("ALL EXPORT CHECKS PASSED")


asyncio.run(main())
