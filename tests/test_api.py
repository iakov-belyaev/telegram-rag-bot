"""Verification of api.py via FastAPI TestClient (mocked control, tmp state files)."""
import pathlib as _pathlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import config

TMP = Path(tempfile.mkdtemp(prefix="api_test_"))
config.TARGET_GROUPS_FILE = TMP / "target_groups.json"
config.STATE_DIR = TMP

from fastapi.testclient import TestClient

import api
import scraper

FAIL = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")
    if not cond:
        FAIL.append(name)


client = TestClient(api.create_app())
TOKEN = config.API_TOKEN
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# /health public
r = client.get("/health")
check("/health public 200", r.status_code == 200 and r.json()["status"] == "ok", extra=str(r.json()))

# auth enforcement
r = client.get("/status")
check("/status without token 401", r.status_code == 401)
r = client.get("/status", headers={"Authorization": "Bearer wrong"})
check("/status wrong token 401", r.status_code == 401)

# /status with token
r = client.get("/status", headers=AUTH)
body = r.json() if r.status_code == 200 else {}
check("/status with token 200 + keys", r.status_code == 200 and "scraper" in body and "config" in body)
import json as _json

_dump = _json.dumps(body)
check("/status config has no secret values",
      config.ADMIN_SECRET not in _dump and config.API_TOKEN not in _dump
      and isinstance(body["config"].get("admin_secret_set"), bool),
      extra=f"dump_len={len(_dump)}")

# /metrics with token (live read-only cluster call)
r = client.get("/metrics", headers=AUTH)
body = r.json() if r.status_code == 200 else {}
check("/metrics 200 with db stats", r.status_code == 200 and body.get("db", {}).get("telegram_messages", {}).get("points", 0) > 0,
      extra=str(body.get("db", {}).get("total_points")))

# /control/scraper pause/resume
control = scraper.get_control()
r = client.post("/control/scraper", headers=AUTH, json={"action": "pause"})
check("control pause", r.status_code == 200 and r.json()["paused"] is True and control.paused is True)
r = client.post("/control/scraper", headers=AUTH, json={"action": "resume"})
check("control resume", r.status_code == 200 and r.json()["paused"] is False and control.paused is False)
r = client.post("/control/scraper", headers=AUTH, json={"action": "explode"})
check("control invalid action 400", r.status_code == 400)

# /control/join
r = client.post("/control/join", headers=AUTH, json={"peer": "@cyprus_api_test"})
check("control join added", r.status_code == 200 and r.json()["added"] is True)
r = client.post("/control/join", headers=AUTH, json={"peer": "@cyprus_api_test"})
check("control join dedupe", r.json()["added"] is False)
r = client.post("/control/join", headers=AUTH, json={"peer": ""})
check("control join empty 400", r.status_code == 400)

# /control/sweep unwired -> 503; wired -> 200
r = client.post("/control/sweep", headers=AUTH)
check("sweep unwired 503", r.status_code == 503)

sweep_ran = {"n": 0}


async def fake_sweep():
    sweep_ran["n"] += 1
    return {"ok": True}


api._sweep_fn = fake_sweep
r = client.post("/control/sweep", headers=AUTH)
check("sweep wired 200", r.status_code == 200 and r.json()["started"] is True)

# token-disabled mode
saved = config.API_TOKEN
config.API_TOKEN = ""
r = client.get("/status", headers=AUTH)
check("empty API_TOKEN -> 503", r.status_code == 503)
config.API_TOKEN = saved

print()
if FAIL:
    print("FAILURES:", FAIL)
    sys.exit(1)
print("ALL API CHECKS PASSED")
