# Spec: `rbac.py` — admin challenge/response authentication (RBAC)

Replace the stub in `rbac.py`. Python 3.11, full type hints, NO prints at import,
NO third-party imports (stdlib only: hmac, hashlib, secrets, time, sys, argparse).
Import-safe and unit-testable; config values are read at CALL TIME (via the `config`
module attribute) so tests can monkeypatch them.

## Concept

The bot's creator (admin) authenticates in the private 1-on-1 chat via an HMAC
challenge/response — possession of the ADMIN_SECRET (server env) + the admin's own
Telegram account are both required. Response codes are single-use (bound to a nonce
with TTL) and rate-limited with lockout.

## 1. AdminAuth class

```python
class AdminAuth:
    """In-memory challenge/response auth + session manager for the admin gateway."""

    def __init__(self) -> None:
        self._challenges: dict[int, dict] = {}   # user_id -> {"nonce": str, "issued_at": float}
        self._sessions: dict[int, float] = {}    # user_id -> expiry epoch
        self._fail_counts: dict[int, int] = {}
        self._lockouts: dict[int, float] = {}    # user_id -> lockout-until epoch
```

Methods (each reads the relevant config.* values AT CALL TIME):

- `def response_for_nonce(self, nonce: str) -> str` — `hmac.new(config.ADMIN_SECRET.encode(), nonce.encode(), hashlib.sha256).hexdigest()[:16]` (lowercase).
- `def issue_challenge(self, user_id: int) -> dict` — generate `nonce = secrets.token_hex(8)`
  (16 hex chars). Store `{"nonce": nonce, "issued_at": time.time()}` (replaces any previous
  challenge for that user). Return:
  `{"nonce": nonce, "expires_in": config.AUTH_CHALLENGE_TTL_SECONDS,
    "how": "Run `python rbac.py respond <nonce>` on the server, then send the printed code here."}`
- `def verify_response(self, user_id: int, response: str) -> tuple[bool, str]` — returns `(ok, reason)`:
  - If locked out (`time.time() < self._lockouts.get(user_id, 0)`) → `(False, f"locked_for:{seconds_left}")` (int seconds).
  - No pending challenge → `(False, "no_challenge")`.
  - Challenge expired (`time.time() - issued_at > config.AUTH_CHALLENGE_TTL_SECONDS`) → delete it → `(False, "expired")`.
  - Normalize response: strip + lowercase. Success when
    `hmac.compare_digest(self.response_for_nonce(nonce), normalized)`:
      → delete challenge, reset fail count, `self._sessions[user_id] = time.time() + config.ADMIN_SESSION_TTL_SECONDS` → `(True, "ok")`.
  - Failure → increment fail count; when `>= config.AUTH_MAX_ATTEMPTS`:
    `self._lockouts[user_id] = time.time() + config.AUTH_LOCKOUT_SECONDS`, reset fail count,
    delete challenge → `(False, f"locked_for:{int(config.AUTH_LOCKOUT_SECONDS)}")`.
    Otherwise → `(False, f"wrong:{remaining_attempts}")` where remaining = MAX - fails.
- `def is_authenticated(self, user_id: int) -> bool` — True when a session exists, not
  expired, and a lockout is not active; on True also SLIDE the expiry forward to
  `time.time() + config.ADMIN_SESSION_TTL_SECONDS` (activity extends the session).
- `def revoke(self, user_id: int) -> bool` — delete session; return whether one existed.
- `def session_status(self, user_id: int) -> dict` — JSON-safe:
  `{"authenticated": bool, "expires_in": int (0 if none), "locked_for": int (0 if none),
    "pending_challenge": bool, "failed_attempts": int}`.

## 2. Singleton

```python
_AUTH = AdminAuth()

def get_auth() -> AdminAuth:
    """Return the module-level AdminAuth singleton."""
```

## 3. CLI helper (admin computes the response code on the server)

```python
def _cli(argv: list[str]) -> int: ...
if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
```
Usage: `python rbac.py respond <nonce>` → prints ONLY the response code (plus a short
`response for nonce <nonce>: <code>` header line to stderr, so stdout stays pipeable).
Unknown/missing args → usage message to stderr, exit code 2. NO other CLI modes.

## 4. Notes

- Never log or store the ADMIN_SECRET anywhere; reply strings never include it.
- All state is in-memory (process lifetime). That is intentional for v2; sessions are
  re-established per process run.
- `response_for_nonce` is public so tests and bot.py help text can reference it.
