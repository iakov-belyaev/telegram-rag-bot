"""Verification of discovery.py: normalization, queue, adapters, agent pipeline."""
import asyncio
import pathlib as _pathlib
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import config

TMP = _pathlib.Path(tempfile.mkdtemp(prefix="discovery_test_"))
config.STATE_DIR = TMP
config.DISCOVERY_QUEUE_FILE = TMP / "discovery_queue.json"
config.DISCOVERY_SEEDS_FILE = TMP / "seeds.yaml"
config.CYPRUS_IT_CHAT = "Cyprus IT"
config.KIPRINFBOT_ENABLED = False
config.WEB_DISCOVERY_ENABLED = False
config.DISCOVERY_AUTO_JOIN = True
config.DISCOVERY_MUTE_JOINED = True
config.DISCOVERY_AUTO_LEAVE_BAD = False

import discovery
import metrics
import scraper
from pyrogram import errors as tg_errors

FAIL = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")
    if not cond:
        FAIL.append(name)


class FakePoint:
    def __init__(self, payload):
        self.payload = payload


async def main():
    # ---------- normalize_peer ----------
    np = discovery.normalize_peer
    check("norm @name", np("@cyprus_it") == "@cyprus_it")
    check("norm t.me/name", np("t.me/cyprus_biz") == "@cyprus_biz")
    check("norm https", np("https://t.me/nicosia_startups") == "@nicosia_startups")
    check("norm invite +hash", np("t.me/+AbCdEf123456") == "https://t.me/+AbCdEf123456")
    check("norm joinchat", np("https://t.me/joinchat/QwErTy12345") == "https://t.me/joinchat/QwErTy12345")
    check("norm numeric", np("-1001234567890") == "-1001234567890")
    check("norm reserved blocked", np("t.me/joinchat") is None)
    check("norm self blocked", np(f"@{config.BOT_USERNAME.lstrip('@')}") is None)
    check("norm short blocked", np("@ab") is None)
    check("norm garbage", np("https://example.com/x") is None)

    # ---------- extract_tme_links ----------
    links = discovery.extract_tme_links(
        "Join https://t.me/cyprus_it and t.me/+AbCdEf123456 plus https://t.me/cyprus_it again."
    )
    check("extract dedupes + orders", links == ["@cyprus_it", "https://t.me/+AbCdEf123456"], extra=str(links))

    # ---------- load_seeds ----------
    config.DISCOVERY_SEEDS_FILE.write_text(
        "peers:\n  - '@seed_one'\n  - 'https://t.me/+SeedHash123'\nqueries:\n  - 'cyprus'\n", encoding="utf-8"
    )
    seeds = discovery.load_seeds()
    check("seeds parsed", seeds["peers"] == ["@seed_one", "https://t.me/+SeedHash123"]
          and seeds["queries"] == ["cyprus"], extra=str(seeds))
    config.DISCOVERY_SEEDS_FILE.unlink()
    missing = discovery.load_seeds()
    check("seeds missing ok", missing["peers"] == [] and missing["queries"] == [])

    # ---------- DiscoveryQueue ----------
    q = discovery.DiscoveryQueue()
    q.load()
    check("queue empty", q.stats()["pending"] == 0)
    added1 = await q.enqueue("@alpha_group", "test")
    added2 = await q.enqueue("@alpha_group", "test")
    check("queue dedupe", added1 is True and added2 is False)
    # persistence
    q2 = discovery.DiscoveryQueue(); q2.load()
    check("queue persists", q2.stats()["pending"] == 1 and q2.stats()["seen"] == 1)
    n = await q2.note_attempt("@alpha_group")
    q3 = discovery.DiscoveryQueue(); q3.load()
    item = q3.pending()[0]
    check("attempts persist", n == 1 and item["attempts"] == 1)
    await q3.mark_done("@alpha_group")
    q4 = discovery.DiscoveryQueue(); q4.load()
    check("mark_done", q4.stats()["pending"] == 0 and q4.stats()["seen"] == 1)

    # ---------- SeedAdapter ----------
    ctx = discovery.DiscoveryContext(queue=q4, client=None, gate=scraper.FloodGate(),
                                     rate=scraper.RateGate(1000, 10),
                                     seeds={"peers": ["@seed_one", "t.me/seed_two", "garbage://x"],
                                            "queries": []})
    seeded = await discovery.SeedAdapter().discover(ctx)
    check("seed adapter enqueues valid only", seeded == 2, extra=str(seeded))

    # ---------- CyprusItAdapter (patched qdrant read) ----------
    points = [
        FakePoint({"text": "join t.me/cyprus_devs — great group", "chat_title": "Cyprus IT"}),
        FakePoint({"text": "check t.me/unrelated_grp", "chat_title": "Random Chat"}),
        FakePoint({"text": "no links here", "chat_title": "Cyprus IT"}),
    ]

    async def fake_qread(default, fn, *args, **kwargs):
        return (points, None)

    discovery.db._qdrant_read = fake_qread
    imported = await discovery.CyprusItAdapter().discover(ctx)
    pend = {i["peer"] for i in q4.pending()}
    check("cyprus-it adapter filters by chat", imported == 1 and "@cyprus_devs" in pend and "@unrelated_grp" not in pend,
          extra=str(pend))

    # ---------- _links_from_message ----------
    msg_btn = SimpleNamespace(
        text="cyprus directory below",
        reply_markup=SimpleNamespace(inline_keyboard=[[
            SimpleNamespace(url="https://t.me/button_group", text="join"),
            SimpleNamespace(url=None, text="plain"),
        ]]),
    )
    btn_links = discovery._links_from_message(msg_btn)
    check("button urls extracted", "@button_group" in btn_links, extra=str(btn_links))

    # ---------- KiprinfobotAdapter (patched collector) ----------
    config.KIPRINFBOT_ENABLED = True
    config.KIPRINFBOT_DELAY_SECONDS = 0.01
    canned = []

    async def fake_collect(client, gate, target, text, timeout):
        return list(canned)

    discovery._send_and_collect = fake_collect
    canned = [SimpleNamespace(text="catalog: t.me/kipri_group_one", reply_markup=None)]
    ctx_with_client = discovery.DiscoveryContext(
        queue=q4, client=object(), gate=ctx.gate, rate=ctx.rate, seeds={"peers": [], "queries": []}
    )
    kip = await discovery.KiprinfobotAdapter().discover(ctx_with_client)
    check("kiprinfobot adapter enqueues", kip == 1 and "@kipri_group_one" in {i["peer"] for i in q4.pending()},
          extra=str(kip))
    config.KIPRINFBOT_ENABLED = False
    check("kiprinfobot disabled skip", await discovery.KiprinfobotAdapter().discover(ctx_with_client) == 0)

    # ---------- WebSearchAdapter ----------
    class FakeProvider(discovery.WebProvider):
        name = "fake"

        async def search(self, query):
            return ["https://t.me/web_cyprus_group", "not a link", "https://t.me/+WebHash123456"]

    config.WEB_DISCOVERY_ENABLED = True
    web = await discovery.WebSearchAdapter(providers=[FakeProvider()]).discover(ctx)
    check("web adapter enqueues", web == 2, extra=str(web))
    config.WEB_DISCOVERY_ENABLED = False
    check("web adapter disabled", await discovery.WebSearchAdapter(providers=[FakeProvider()]).discover(ctx) == 0)

    # ---------- GroupDiscoveryAgent ----------
    mute_calls = []
    classify_map = {}

    async def fake_mute(client, chat_id):
        mute_calls.append(chat_id)
        return True

    async def fake_classify(client, gate, chat):
        label = classify_map.get(getattr(chat, "id", None), "GOOD")
        return {"chat_id": getattr(chat, "id", None), "title": getattr(chat, "title", ""),
                "label": label, "score": 0.9, "reasons": ["test"], "sampled_messages": 10,
                "checked_at": discovery.time.time()}

    discovery.mute_chat = fake_mute
    discovery.classify_chat = fake_classify

    class AgentClient:
        IDS = {"@new_good": -100901, "@new_bad": -100902}

        async def join_chat(self, peer):
            if peer == "@perm_bad":
                raise tg_errors.UsernameNotOccupied(peer)
            return SimpleNamespace(id=self.IDS.get(peer, -100900), title=f"Joined {peer}")

        async def get_chat(self, peer):
            return SimpleNamespace(id=self.IDS.get(peer, -100900), title=f"Chat {peer}")

    classify_map = {-100901: "GOOD", -100902: "SCAM"}
    client = AgentClient()
    queue = discovery.DiscoveryQueue(); queue.load()
    cache = discovery.QualityCache(); cache.load()
    targets = scraper.TargetManager(); targets.load()
    control = scraper.get_control()
    agent = discovery.GroupDiscoveryAgent(client, scraper.FloodGate(), scraper.RateGate(1000, 10),
                                          queue, cache, targets, control)

    await queue.enqueue("@new_good", "seed")
    await queue.enqueue("@new_bad", "seed")
    await queue.enqueue("@perm_bad", "seed")

    r1 = await agent.process_candidate({"peer": "@new_good", "source": "seed", "attempts": 0})
    check("agent joins+mutes+labels GOOD", r1["state"] == "joined" and r1["label"] == "GOOD"
          and mute_calls, extra=str(r1))
    check("GOOD verdict cached", (cache.get(-100901) or {}).get("label") == "GOOD")

    r2 = await agent.process_candidate({"peer": "@new_bad", "source": "seed", "attempts": 0})
    check("agent rejects SCAM", r2["label"] == "SCAM" and metrics.get_counter("groups_rejected") >= 1,
          extra=str(r2))
    tstate = {t["peer"]: t for t in targets.all()}
    check("targets note has label", "SCAM" in (tstate.get("@new_bad", {}).get("note") or ""))

    r3 = await agent.process_candidate({"peer": "@perm_bad", "source": "seed", "attempts": 0})
    check("permanent join error handled", r3["state"] == "join_error"
          and metrics.get_counter("groups_join_rejected") >= 1, extra=str(r3))

    # attempts cap
    await queue.enqueue("@flaky_grp", "seed")
    await queue.note_attempt("@flaky_grp"); await queue.note_attempt("@flaky_grp"); await queue.note_attempt("@flaky_grp")
    r4 = await agent.process_candidate({"peer": "@flaky_grp", "source": "seed", "attempts": 3})
    check("attempts cap drops", r4["state"] == "dropped")

    # run_cycle end-to-end (adapters offline-safe: kiprinfobot/web disabled, seeds empty)
    await queue.enqueue("@cycle_grp", "seed")
    classify_map[-100900] = "GOOD"
    stats = await agent.run_cycle(seeds={"peers": [], "queries": []})
    check("run_cycle processes pending", stats["processed"] >= 1, extra=str(stats))

    print()
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)
    print("ALL DISCOVERY CHECKS PASSED")


asyncio.run(main())
