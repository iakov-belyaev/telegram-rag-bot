"""Watchlist: alert when tracked entities appear in newly ingested messages.

Tokens are matched case-insensitively against message text, with a digits-only
fallback for phone-like tokens. Hits are appended to a JSONL queue; main.py
drains the queue and DMs the admin. Self-contained (no imports from query/
discovery/scraper) so ingestion code can call it without import cycles.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import config
import metrics
from logging_config import get_logger

_logger = get_logger("watchlist")


def log(message: str) -> None:
    _logger.info("%s", message)


def _atomic_write_json(path: str, data: Any) -> None:
    config.ensure_state_dir()
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class Watchlist:
    """Persistent list of tracked tokens. File: config.WATCHLIST_FILE."""

    def __init__(self) -> None:
        self._data: dict = {"entities": []}
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = config.WATCHLIST_FILE
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("entities"), list):
                self._data = data
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            log(f"⚠️ watchlist load failed: {e}")

    def all(self) -> list[dict]:
        self.load()
        return list(self._data["entities"])

    def tokens(self) -> list[str]:
        return [str(e.get("token", "")) for e in self.all() if e.get("token")]

    async def add(self, token: str, note: str = "") -> bool:
        self.load()
        normalized = token.strip()
        if not normalized:
            return False
        if any(str(e.get("token", "")).lower() == normalized.lower() for e in self._data["entities"]):
            return False
        self._data["entities"].append({
            "token": normalized,
            "note": note,
            "added_at": datetime.now(timezone.utc).isoformat(),
        })
        _atomic_write_json(str(config.WATCHLIST_FILE), self._data)
        return True

    async def remove(self, token: str) -> bool:
        self.load()
        before = len(self._data["entities"])
        self._data["entities"] = [
            e for e in self._data["entities"]
            if str(e.get("token", "")).lower() != token.strip().lower()
        ]
        if len(self._data["entities"]) == before:
            return False
        _atomic_write_json(str(config.WATCHLIST_FILE), self._data)
        return True


def match_message(payload: dict, tokens: list[str]) -> list[str]:
    """Return the tokens present in a message payload (pure, unit-testable).

    Two paths: (1) numeric >=6 digits compared against the text's digits
    (formatting-insensitive, catches phone variants); (2) letter-bearing tokens
    matched as plain substrings. Purely-numeric short tokens never match —
    they would fire on every message containing the digits sequence.
    """
    text = str(payload.get("text", "") or "").lower()
    if not text or not tokens:
        return []
    text_digits = re.sub(r"\D", "", text)
    matched: list[str] = []
    for token in tokens:
        needle = token.lower().lstrip("@")
        if not needle:
            continue
        needle_digits = re.sub(r"\D", "", needle)
        has_letters = any(c.isalpha() for c in needle)
        if len(needle_digits) >= 6 and needle_digits in text_digits:
            matched.append(token)
            continue
        if has_letters and needle in text:
            matched.append(token)
    return matched


async def check_payload(payload: dict) -> list[dict]:
    """Check a message payload against the watchlist; queue + return hits."""
    try:
        wl = get_watchlist()
        tokens = wl.tokens()
        if not tokens:
            return []
        matched = match_message(payload, tokens)
        if not matched:
            return []
        now = datetime.now(timezone.utc).isoformat()
        hits = [{
            "ts": now,
            "token": token,
            "chat_id": payload.get("chat_id"),
            "chat_title": payload.get("chat_title"),
            "message_id": payload.get("message_id"),
            "sender_name": payload.get("sender_name"),
            "sender_username": payload.get("sender_username"),
            "link": payload.get("link"),
            "excerpt": str(payload.get("text", ""))[:200],
        } for token in matched]
        config.ensure_state_dir()
        with open(config.WATCHLIST_HITS_FILE, "a", encoding="utf-8") as f:
            for hit in hits:
                f.write(json.dumps(hit, ensure_ascii=False) + "\n")
        metrics.inc("watchlist_hits", len(hits))
        return hits
    except Exception as e:  # noqa: BLE001
        log(f"⚠️ watchlist check failed: {e}")
        return []


async def drain_hits(send_fn: Optional[Callable] = None) -> int:
    """Read + clear the hits queue; deliver via send_fn(text) when provided."""
    path = Path(config.WATCHLIST_HITS_FILE)
    if not path.exists():
        return 0
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
    except Exception as e:  # noqa: BLE001
        log(f"⚠️ watchlist drain read failed: {e}")
        return 0
    if not lines:
        return 0
    delivered = 0
    if send_fn is not None:
        for line in lines[:20]:  # cap per drain to avoid DM flooding
            try:
                hit = json.loads(line)
                text = (
                    f"🎯 WATCHLIST HIT: {hit.get('token')}\n"
                    f"{hit.get('sender_name')} (@{hit.get('sender_username') or '-'}) "
                    f"in {hit.get('chat_title')}\n{hit.get('excerpt')}\n{hit.get('link') or ''}"
                )
                await send_fn(text)
                delivered += 1
            except Exception as e:  # noqa: BLE001
                log(f"⚠️ watchlist alert send failed: {e}")
    try:
        path.write_text("", encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log(f"⚠️ watchlist drain clear failed: {e}")
    return delivered if send_fn is not None else len(lines)


_WATCHLIST: Optional[Watchlist] = None


def get_watchlist() -> Watchlist:
    global _WATCHLIST
    if _WATCHLIST is None:
        _WATCHLIST = Watchlist()
        _WATCHLIST.load()
    return _WATCHLIST


def reset_watchlist() -> None:
    global _WATCHLIST
    _WATCHLIST = None
