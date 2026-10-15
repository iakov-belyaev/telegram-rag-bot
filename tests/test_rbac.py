"""Verification of rbac.py challenge/response auth (stdlib only, no network)."""
import pathlib as _pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import config
import rbac

FAIL = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")
    if not cond:
        FAIL.append(name)


auth = rbac.AdminAuth()

# 1. deterministic response
code1 = auth.response_for_nonce("deadbeefdeadbeef")
code2 = auth.response_for_nonce("deadbeefdeadbeef")
check("response deterministic", code1 == code2 and len(code1) == 16 and all(c in "0123456789abcdef" for c in code1))

# 2. issue + wrong attempts (user 101; default MAX=3)
ch = auth.issue_challenge(101)
check("challenge issued", ch["nonce"] != "" and ch["expires_in"] > 0)
ok, reason = auth.verify_response(101, "0000000000000000")
check("wrong response rejected", ok is False and reason == "wrong:2", extra=reason)
ok, reason = auth.verify_response(101, "ffffffffffffffff")
check("second wrong", ok is False and reason == "wrong:1", extra=reason)

# 3. correct response on remaining challenge
good = auth.response_for_nonce(ch["nonce"])
ok, reason = auth.verify_response(101, good.upper())  # case-insensitive
check("correct response accepted", ok is True and reason == "ok", extra=reason)
check("session authenticated", auth.is_authenticated(101) is True)
check("challenge single-use", auth.verify_response(101, good) == (False, "no_challenge"))
st = auth.session_status(101)
check("session_status shape", st["authenticated"] is True and st["pending_challenge"] is False and st["expires_in"] > 0,
      extra=str(st))

# 4. revoke
check("revoke", auth.revoke(101) is True and auth.is_authenticated(101) is False)

# 5. lockout (user 202, MAX=2)
config.AUTH_MAX_ATTEMPTS = 2
config.AUTH_LOCKOUT_SECONDS = 60
auth.issue_challenge(202)
auth.verify_response(202, "beef")
ok, reason = auth.verify_response(202, "beef")
check("lockout after max attempts", ok is False and reason.startswith("locked_for:"), extra=reason)
ok, reason = auth.verify_response(202, "beef")
check("locked while locked out", ok is False and reason.startswith("locked_for:"), extra=reason)
auth._sessions[202] = time.time() + 9999  # even a valid session is blocked during lockout
check("lockout blocks session", auth.is_authenticated(202) is False)
config.AUTH_MAX_ATTEMPTS = 3
config.AUTH_LOCKOUT_SECONDS = 300

# 6. challenge expiry (user 303)
config.AUTH_CHALLENGE_TTL_SECONDS = 0
ch3 = auth.issue_challenge(303)
code3 = auth.response_for_nonce(ch3["nonce"])
ok, reason = auth.verify_response(303, code3)
check("expired challenge", ok is False and reason == "expired", extra=reason)
config.AUTH_CHALLENGE_TTL_SECONDS = 180

# 7. session TTL slide (user 404)
config.ADMIN_SESSION_TTL_SECONDS = 1
ch4 = auth.issue_challenge(404)
auth.verify_response(404, auth.response_for_nonce(ch4["nonce"]))
time.sleep(0.6)
check("session slides on activity", auth.is_authenticated(404) is True)
exp = auth._sessions[404]
check("expiry extended", exp > time.time() + 0.5)
config.ADMIN_SESSION_TTL_SECONDS = 2700

# 8. CLI helper via subprocess (real .env ADMIN_SECRET)
nonce = "cafebabe12345678"
proc = subprocess.run(
    [sys.executable, str(_pathlib.Path(__file__).resolve().parent.parent / "rbac.py"), "respond", nonce],
    capture_output=True, text=True, cwd=str(_pathlib.Path(__file__).resolve().parent.parent),
)
cli_code = proc.stdout.strip()
expected = auth.response_for_nonce(nonce)
check("CLI respond works", proc.returncode == 0 and cli_code == expected, extra=f"cli={cli_code[:8]}... exp={expected[:8]}...")
proc2 = subprocess.run(
    [sys.executable, str(_pathlib.Path(__file__).resolve().parent.parent / "rbac.py")],
    capture_output=True, text=True, cwd=str(_pathlib.Path(__file__).resolve().parent.parent),
)
check("CLI usage exit 2", proc2.returncode == 2)

print()
if FAIL:
    print("FAILURES:", FAIL)
    sys.exit(1)
print("ALL RBAC CHECKS PASSED")
