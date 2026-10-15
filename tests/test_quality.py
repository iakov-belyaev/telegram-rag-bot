"""Verification of quality.py: features, scoring rules, cache, classifier."""
import asyncio
import pathlib as _pathlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import config

TMP = _pathlib.Path(tempfile.mkdtemp(prefix="quality_test_"))
config.QUALITY_CACHE_FILE = TMP / "quality_cache.json"
config.STATE_DIR = TMP

import quality

FAIL = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")
    if not cond:
        FAIL.append(name)


def msg(text, url=False, fwd=False, when=None):
    return {"text": text, "source": "text", "has_url": url or ("http" in text) or ("t.me" in text),
            "is_forward": fwd, "date": when or datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)}


def feat(**kw):
    base = {"scam_hits": 0, "spam_hits": 0, "link_density": 0.0, "duplicate_ratio": 0.0,
            "forward_ratio": 0.0, "text_ratio": 1.0, "velocity": 1.0, "member_count": 100,
            "message_count": 10}
    base.update(kw)
    return base


async def main():
    # ---------- pure features ----------
    msgs = [msg("hello there friends how are you"), msg("short"), msg("https://spam.io/x", url=True), msg("", )]
    check("text_ratio", abs(quality.text_ratio(msgs) - 0.5) < 1e-9, extra=str(quality.text_ratio(msgs)))
    check("link_density", abs(quality.link_density(msgs) - 0.25) < 1e-9)
    dupes = [msg("join now bonus"), msg("JOIN NOW BONUS "), msg("different text here")]
    check("duplicate_ratio", quality.duplicate_ratio(dupes) > 0.6, extra=str(quality.duplicate_ratio(dupes)))
    t0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    vel_msgs = [msg("a" * 12, when=t0), msg("b" * 12, when=t0 + timedelta(minutes=1))]
    check("velocity ~120/h", abs(quality.velocity_per_hour(vel_msgs) - 120) < 1, extra=str(quality.velocity_per_hour(vel_msgs)))
    hits = quality.pattern_hits([msg("guaranteed returns guaranteed profit")], quality.SCAM_PATTERNS)
    check("pattern_hits counts", sum(hits.values()) >= 2, extra=str(hits))
    check("empty features -> UNKNOWN", quality.score_sample(feat(message_count=0))[0] == quality.UNKNOWN)

    # ---------- scoring rules ----------
    check("rule SCAM (2 hits)", quality.score_sample(feat(scam_hits=2))[0] == quality.SCAM)
    check("rule SCAM (1 hit + links)", quality.score_sample(feat(scam_hits=1, link_density=0.4))[0] == quality.SCAM)
    check("rule SPAM (3 spam hits)", quality.score_sample(feat(spam_hits=3))[0] == quality.SPAM)
    check("rule SPAM (links+dupes)", quality.score_sample(feat(link_density=0.7, duplicate_ratio=0.5))[0] == quality.SPAM)
    check("rule JUNK (dupes)", quality.score_sample(feat(duplicate_ratio=0.8))[0] == quality.JUNK)
    check("rule JUNK (media-only)", quality.score_sample(feat(text_ratio=0.1))[0] == quality.JUNK)
    label, score, reasons = quality.score_sample(feat(spam_hits=1, link_density=0.2))
    check("rule GOOD", label == quality.GOOD and score >= 0.5, extra=f"{label} {score:.2f}")
    check("label_allows_indexing", quality.label_allows_indexing(quality.GOOD) is True
          and quality.label_allows_indexing(quality.SCAM) is False)

    # ---------- cache ----------
    cache = quality.QualityCache()
    cache.load()
    check("cache miss", cache.get(42) is None)
    await cache.set(42, {"label": quality.GOOD, "score": 0.9, "checked_at": quality.time.time()})
    got = cache.get(42)
    check("cache roundtrip", got is not None and got["label"] == quality.GOOD)
    check("cache file written", config.QUALITY_CACHE_FILE.exists())

    # ---------- classify_chat ----------
    def make_client(texts, raise_history=False):
        class C:
            async def get_chat_members_count(self, cid):
                return 250

            async def get_chat_members(self, cid, filter=None, **kw):
                yield SimpleNamespace(user=SimpleNamespace(username="admin_one"))
                yield SimpleNamespace(user=SimpleNamespace(username=None))

            async def get_chat_history(self, cid, limit=50, **kw):
                if raise_history:
                    raise RuntimeError("history unavailable")
                for t in texts:
                    yield SimpleNamespace(text=t, caption=None, forward_date=None,
                                          date=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))

        return C()

    gate = quality.FloodGate()
    chat = SimpleNamespace(id=1001, title="Cyprus Developers")

    good = await quality.classify_chat(make_client([
        "Anyone knows a good accountant in Nicosia?",
        "We meet every Thursday, see you there",
        "The new immigration rules update is out",
        "Startup pitch night was great yesterday",
    ]), gate, chat)
    check("classify GOOD chat", good["label"] == quality.GOOD, extra=str(good["reasons"]))
    check("classify result shape", {"chat_id", "title", "label", "score", "reasons",
                                    "sampled_messages", "checked_at"} <= set(good.keys()))

    scam_chat = SimpleNamespace(id=1002, title="Crypto Gold Signals")
    scam = await quality.classify_chat(make_client([
        "GUARANTEED RETURNS — double your money in a week",
        "DM me to profit while it lasts, double your deposit",
        "guaranteed profit guaranteed returns",
    ]), gate, scam_chat)
    check("classify SCAM chat", scam["label"] == quality.SCAM, extra=str(scam["reasons"]))

    broken = await quality.classify_chat(make_client([], raise_history=True), gate,
                                         SimpleNamespace(id=1003, title="Unreachable"))
    check("unreachable history -> UNKNOWN", broken["label"] == quality.UNKNOWN,
          extra=str(broken["reasons"]))

    # ---------- LLM hook ----------
    config.QUALITY_LLM_ENABLED = True

    async def scam_llm(prompt):
        return '```json\n{"label": "SCAM", "confidence": 0.95, "reason": "fake investment"}\n```'

    llm_verdict = await quality.classify_chat(make_client([
        "hello friends, how is everyone today?",
        "lovely weather this week indeed",
    ]), gate, SimpleNamespace(id=1004, title="Ambiguous"), llm=scam_llm)
    check("llm override applied", llm_verdict["label"] == quality.SCAM
          and any("llm:" in r for r in llm_verdict["reasons"]), extra=str(llm_verdict["reasons"]))

    async def broken_llm(prompt):
        raise RuntimeError("no llm today")

    llm_err = await quality.classify_chat(make_client([
        "hello friends, how is everyone today?",
        "lovely weather this week indeed",
    ]), gate, SimpleNamespace(id=1005, title="Still Fine"), llm=broken_llm)
    check("llm error falls back to rules", llm_err["label"] == quality.GOOD
          and "llm-error" in llm_err["reasons"])
    config.QUALITY_LLM_ENABLED = False

    print()
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)
    print("ALL QUALITY CHECKS PASSED")


asyncio.run(main())
