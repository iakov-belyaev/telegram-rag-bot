"""Phase 1 regression tests: get_chat_photos fix, resolve_user ladder, metrics, /prometheus."""
import asyncio
import hashlib
import io
import pathlib as _pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import config
import metrics
import profiles
from pyrogram import errors as tg_errors

FAIL = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")
    if not cond:
        FAIL.append(name)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def gate():
    return profiles.FloodGate()


# ---------- metrics module ----------
metrics.inc("t_probe", 2.0)
check("metrics counter", metrics.get_counter("t_probe") == 2.0)
snap = metrics.snapshot()
check("metrics snapshot json-safe", isinstance(snap["counters"], dict) and "t_probe" in snap["counters"])
prom = metrics.render_prometheus()
check("prometheus render", "# TYPE tgbot_t_probe counter" in prom and "tgbot_t_probe 2.0" in prom)


# ---------- resolve_user ladder ----------
class DirectClient:
    async def get_users(self, uid):
        return SimpleNamespace(id=uid, username="ok")


class ContextClient:
    async def get_users(self, uid):
        raise tg_errors.PeerIdInvalid(uid)

    async def get_chat_members(self, cid, **kw):
        yield SimpleNamespace(user=SimpleNamespace(id=1))
        yield SimpleNamespace(user=SimpleNamespace(id=77, username="ctx"))

    async def get_chat(self, uid):
        raise AssertionError("should not be reached")


class WarmupClient:
    def __init__(self):
        self.warmed = False

    async def get_users(self, uid):
        if not self.warmed:
            raise tg_errors.PeerIdInvalid(uid)
        return SimpleNamespace(id=uid, username="warm")

    async def get_chat_members(self, cid, **kw):
        raise RuntimeError("no member access")

    async def get_chat(self, peer):
        self.warmed = True
        return SimpleNamespace(id=peer)


class DeadClient:
    async def get_users(self, uid):
        raise tg_errors.PeerIdInvalid(uid)

    async def get_chat(self, uid):
        raise RuntimeError("nothing works")


async def main():
    # direct path
    u = await profiles.resolve_user(DirectClient(), gate(), 5)
    check("resolve_user direct", u is not None and u.username == "ok")

    # context scan path
    before_ctx = metrics.get_counter("peers_resolved_via_context")
    c = await profiles.resolve_user(ContextClient(), gate(), 77, context_chat_id=-100)
    check("resolve_user via context", c is not None and c.username == "ctx",
          extra=f"scanned-hit for 77")
    check("context metric bumped", metrics.get_counter("peers_resolved_via_context") == before_ctx + 1)

    # warmup path
    w = await profiles.resolve_user(WarmupClient(), gate(), 88)
    check("resolve_user via warmup", w is not None and w.username == "warm")

    # dead end -> None + metric
    before_unres = metrics.get_counter("peer_unresolved")
    d = await profiles.resolve_user(DeadClient(), gate(), 99)
    check("resolve_user dead end returns None", d is None)
    check("peer_unresolved metric", metrics.get_counter("peer_unresolved") == before_unres + 1)

    # ---------- enrich_user photo path (get_chat_photos async generator) ----------
    class PhotoClient:
        async def get_users(self, uid):
            return SimpleNamespace(id=uid, first_name="A", last_name="B", username="ab",
                                   bio="bio", is_bot=False, is_premium=False)

        async def get_chat_photos(self, uid, limit=0):
            yield SimpleNamespace(sizes=[
                SimpleNamespace(width=640, height=640, file_size=300, file_id="big"),
                SimpleNamespace(width=160, height=160, file_size=20, file_id="small"),
            ])

        async def download_media(self, file_id, in_memory=False):
            assert file_id == "small", f"expected smallest size, got {file_id}"
            assert in_memory is True
            return io.BytesIO(b"fake-image-bytes")

    info = await profiles.enrich_user(PhotoClient(), gate(), 123, None)
    expected_hash = hashlib.sha256(b"fake-image-bytes").hexdigest()[: config.PFP_HASH_SIZE // 4]
    check("photo: smallest size downloaded + hashed", info["pfp_hash"] == expected_hash,
          extra=f"hash={info['pfp_hash'][:12]}...")
    check("photo: meta recorded", info["pfp_meta"] == {"width": 160, "height": 160, "file_size": 20})
    check("photo: refreshed", info["refreshed"] is True)

    # ---------- failure path: client WITHOUT get_chat_photos -> swallowed + metric ----------
    class NoPhotoClient:
        async def get_users(self, uid):
            return SimpleNamespace(id=uid, first_name="X", bio="", is_bot=False, is_premium=False)

    before_fail = metrics.get_counter("profile_photo_failures")
    info2 = await profiles.enrich_user(NoPhotoClient(), gate(), 124, None)
    check("photo failure swallowed", info2["pfp_hash"] == "" and info2["refreshed"] is True)
    check("photo failure metric", metrics.get_counter("profile_photo_failures") == before_fail + 1)

    # ---------- force_refresh skips the freshness fast-path ----------
    existing = {
        "updated_at": now_iso(), "bio": "cached", "pfp_hash": "cafe",
        "pfp_meta": {}, "first_name": "", "last_name": "", "username": "",
        "is_bot": False, "is_premium": False,
    }
    info_fresh = await profiles.enrich_user(PhotoClient(), gate(), 123, existing, force_refresh=False)
    check("fresh cache reused without force", info_fresh["refreshed"] is False and info_fresh["bio"] == "cached")
    info_forced = await profiles.enrich_user(PhotoClient(), gate(), 123, existing, force_refresh=True)
    check("force_refresh bypasses cache", info_forced["refreshed"] is True and info_forced["bio"] == "bio")

    # ---------- api /prometheus route ----------
    import api
    from fastapi.testclient import TestClient

    client = TestClient(api.create_app())
    r = client.get("/prometheus", headers={"Authorization": f"Bearer {config.API_TOKEN}"})
    check("/prometheus 200 + text", r.status_code == 200 and "tgbot_" in r.text,
          extra=f"status={r.status_code}")
    r2 = client.get("/prometheus")
    check("/prometheus without token 401", r2.status_code == 401)

    print()
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)
    print("ALL PHASE1 CHECKS PASSED")


asyncio.run(main())
