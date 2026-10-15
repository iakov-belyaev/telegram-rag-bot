"""Admin challenge/response authentication (RBAC) for telegram-rag-bot.

Stdlib-only, import-safe. The creator authenticates in the private chat via an
HMAC challenge/response bound to a single-use nonce, with attempt limits and
lockout. The matching response code is computed server-side via the CLI helper:
    python rbac.py respond <nonce>
"""

import argparse
import hashlib
import hmac
import secrets
import sys
import time

import config


class AdminAuth:
    """In-memory challenge/response auth + session manager for the admin gateway."""

    def __init__(self) -> None:
        self._challenges: dict[int, dict] = {}   # user_id -> {"nonce": str, "issued_at": float}
        self._sessions: dict[int, float] = {}    # user_id -> expiry epoch
        self._fail_counts: dict[int, int] = {}
        self._lockouts: dict[int, float] = {}    # user_id -> lockout-until epoch

    def response_for_nonce(self, nonce: str) -> str:
        """Compute the expected response code for a nonce (lowercase hex, 16 chars)."""
        return hmac.new(
            config.ADMIN_SECRET.encode(),
            nonce.encode(),
            hashlib.sha256,
        ).hexdigest()[:16]

    def issue_challenge(self, user_id: int) -> dict:
        """Issue a fresh single-use challenge for the given user."""
        nonce = secrets.token_hex(8)
        self._challenges[user_id] = {"nonce": nonce, "issued_at": time.time()}
        return {
            "nonce": nonce,
            "expires_in": config.AUTH_CHALLENGE_TTL_SECONDS,
            "how": (
                "Run `python rbac.py respond <nonce>` on the server, "
                "then send the printed code here."
            ),
        }

    def verify_response(self, user_id: int, response: str) -> tuple[bool, str]:
        """Verify a response code against the pending challenge. Returns (ok, reason)."""
        now = time.time()

        lockout_until = self._lockouts.get(user_id, 0)
        if now < lockout_until:
            return (False, f"locked_for:{int(lockout_until - now)}")

        challenge = self._challenges.get(user_id)
        if challenge is None:
            return (False, "no_challenge")

        if now - challenge["issued_at"] > config.AUTH_CHALLENGE_TTL_SECONDS:
            del self._challenges[user_id]
            return (False, "expired")

        normalized = response.strip().lower()
        expected = self.response_for_nonce(challenge["nonce"])

        if hmac.compare_digest(expected, normalized):
            del self._challenges[user_id]
            self._fail_counts[user_id] = 0
            self._sessions[user_id] = now + config.ADMIN_SESSION_TTL_SECONDS
            return (True, "ok")

        fails = self._fail_counts.get(user_id, 0) + 1
        self._fail_counts[user_id] = fails

        if fails >= config.AUTH_MAX_ATTEMPTS:
            self._lockouts[user_id] = now + config.AUTH_LOCKOUT_SECONDS
            self._fail_counts[user_id] = 0
            del self._challenges[user_id]
            return (False, f"locked_for:{int(config.AUTH_LOCKOUT_SECONDS)}")

        remaining = config.AUTH_MAX_ATTEMPTS - fails
        return (False, f"wrong:{remaining}")

    def is_authenticated(self, user_id: int) -> bool:
        """Return True when the user has a live session; slides the expiry forward."""
        now = time.time()

        if now < self._lockouts.get(user_id, 0):
            return False

        expiry = self._sessions.get(user_id)
        if expiry is None or now >= expiry:
            return False

        self._sessions[user_id] = now + config.ADMIN_SESSION_TTL_SECONDS
        return True

    def revoke(self, user_id: int) -> bool:
        """Revoke the user's session. Returns whether one existed."""
        return self._sessions.pop(user_id, None) is not None

    def session_status(self, user_id: int) -> dict:
        """Return a JSON-safe snapshot of the user's auth state."""
        now = time.time()

        expiry = self._sessions.get(user_id)
        expires_in = int(expiry - now) if expiry is not None and expiry > now else 0

        lockout_until = self._lockouts.get(user_id, 0)
        locked_for = int(lockout_until - now) if lockout_until > now else 0

        return {
            "authenticated": self.is_authenticated(user_id),
            "expires_in": expires_in,
            "locked_for": locked_for,
            "pending_challenge": user_id in self._challenges,
            "failed_attempts": self._fail_counts.get(user_id, 0),
        }


_AUTH = AdminAuth()


def get_auth() -> AdminAuth:
    """Return the module-level AdminAuth singleton."""
    return _AUTH


def _cli(argv: list[str]) -> int:
    """CLI helper: `python rbac.py respond <nonce>` prints the response code."""
    parser = argparse.ArgumentParser(
        prog="rbac.py",
        description="Admin challenge/response helper.",
    )
    subparsers = parser.add_subparsers(dest="command")

    respond = subparsers.add_parser("respond", help="Compute the response code for a nonce.")
    respond.add_argument("nonce", help="The nonce issued by the bot.")

    args = parser.parse_args(argv)

    if args.command != "respond":
        parser.print_usage(sys.stderr)
        return 2

    code = _AUTH.response_for_nonce(args.nonce)
    print(f"response for nonce {args.nonce}: {code}", file=sys.stderr)
    print(code)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
