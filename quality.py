"""Chat quality classifier: GOOD / SPAM / SCAM / JUNK before indexing.

Heuristic feature extraction (scam/spam patterns, link density, duplicates,
velocity) with an optional pluggable LLM hook. Only GOOD chats may enter the
OSINT store; UNKNOWN verdicts fall back to the legacy chat filter.
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import config
import metrics
from logging_config import get_logger
from scraper import FloodGate, iterate_with_flood, telegram_call

_logger = get_logger("quality")


def log(message: str) -> None:
    _logger.info("%s", message)


# ==========================================
# 1. CONSTANTS
# ==========================================
GOOD, SPAM, SCAM, JUNK, UNKNOWN = "GOOD", "SPAM", "SCAM", "JUNK", "UNKNOWN"
QUALITY_CACHE_TTL_SECONDS = 7 * 86400

SCAM_PATTERNS = [  # regex fragments, case-insensitive; 2+ hits (or 1 + high links) => SCAM
    r"guaranteed\s+(returns?|profit)",
    r"double\s+your\s+(money|investment|deposit)",
    r"crypto\s+giveaway",
    r"dm\s+me\s+(to|for)\s+(invest|profit|earn)",
    r"инвестируй",
    r"сигналы\s+бесплатно",
    r"\bх2\b",
    r"bookmaker",
    r"no\s+risk\s+profit",
    r"withdraw\s+instantly",
    r"100%\s+profit",
    r"пассивный\s+доход",
]
SPAM_PATTERNS = [  # lighter marketing spam
    r"buy\s+followers",
    r"promo\s+code",
    r"discount\s+\d+%",
    r"casino",
    r"\bbetting\b",
    r"t\.me/[+]",
    r"подписывайтесь",
    r"реклама",
    r"\bsub4sub\b",
]
URL_REGEX = re.compile(r"https?://|t\.me/|www\.", re.IGNORECASE)

_SCAM_REGEXES = [re.compile(p, re.IGNORECASE) for p in SCAM_PATTERNS]
_SPAM_REGEXES = [re.compile(p, re.IGNORECASE) for p in SPAM_PATTERNS]
_NON_ALNUM_REGEX = re.compile(r"[^a-z0-9]+")


# ==========================================
# 2. DATA GATHERING (TELEGRAM SIDE)
# ==========================================
async def gather_chat_sample(client, gate: FloodGate, chat, limit: int = 0) -> dict:
    """Best-effort sample of a chat's metadata and recent messages.

    Every Telegram call is individually guarded; failures are recorded in
    ``errors`` and gathering continues. Never raises.
    """
    if not limit:
        limit = config.QUALITY_SAMPLE_SIZE

    sample: dict = {
        "chat_id": getattr(chat, "id", 0),
        "title": getattr(chat, "title", "") or "",
        "username": getattr(chat, "username", "") or "",
        "member_count": None,
        "admin_usernames": [],
        "messages": [],
        "errors": [],
    }

    # --- member count ---
    try:
        sample["member_count"] = await telegram_call(
            gate, client.get_chat_members_count, chat.id
        )
    except Exception as e:
        sample["errors"].append(f"member_count:{e}")

    # --- administrators ---
    try:
        admins: list[str] = []
        async for member in iterate_with_flood(
            gate, client.get_chat_members, chat.id, filter="administrators"
        ):
            username = getattr(getattr(member, "user", None), "username", None)
            if username:
                admins.append(username)
            if len(admins) >= 25:
                break
        sample["admin_usernames"] = admins
    except Exception as e:
        sample["errors"].append(f"admins:{e}")

    # --- recent messages ---
    try:
        messages: list[dict] = []
        async for message in iterate_with_flood(
            gate, client.get_chat_history, chat.id, limit=limit
        ):
            text = (getattr(message, "text", None) or "").strip()
            source = "text"
            if not text:
                text = (getattr(message, "caption", None) or "").strip()
                source = "caption" if text else "text"
            messages.append({
                "text": text,
                "source": source,
                "has_url": bool(URL_REGEX.search(text)),
                "is_forward": bool(getattr(message, "forward_date", None)),
                "date": getattr(message, "date", None),
            })
            if len(messages) >= limit:
                break
        sample["messages"] = messages
    except Exception as e:
        sample["errors"].append(f"history:{e}")

    return sample


# ==========================================
# 3. PURE FEATURE FUNCTIONS
# ==========================================
def text_ratio(messages: list[dict]) -> float:
    """Fraction of messages whose stripped text is at least 10 chars."""
    if not messages:
        return 0.0
    good = sum(1 for m in messages if len((m.get("text") or "").strip()) >= 10)
    return good / len(messages)


def link_density(messages: list[dict]) -> float:
    """Fraction of messages containing a URL."""
    if not messages:
        return 0.0
    return sum(1 for m in messages if m.get("has_url")) / len(messages)


def forward_ratio(messages: list[dict]) -> float:
    """Fraction of forwarded messages."""
    if not messages:
        return 0.0
    return sum(1 for m in messages if m.get("is_forward")) / len(messages)


def _normalize_text(text: str) -> str:
    """Lowercase, strip URLs, collapse non-alphanumerics."""
    stripped = URL_REGEX.sub(" ", text or "")
    return _NON_ALNUM_REGEX.sub(" ", stripped.lower()).strip()


def duplicate_ratio(messages: list[dict]) -> float:
    """Fraction of normalized texts that repeat within the sample."""
    if not messages:
        return 0.0
    normalized = [_normalize_text(m.get("text") or "") for m in messages]
    normalized = [t for t in normalized if t]
    if not normalized:
        return 0.0
    counts: dict[str, int] = {}
    for t in normalized:
        counts[t] = counts.get(t, 0) + 1
    dupes = sum(c for c in counts.values() if c > 1)
    return dupes / len(normalized)


def _to_epoch(value: Any) -> Optional[float]:
    """Best-effort conversion of a telegram date to a POSIX timestamp."""
    if value is None:
        return None
    if isinstance(value, datetime):
        try:
            return value.timestamp()
        except Exception:
            return None
    if hasattr(value, "timestamp"):
        try:
            return float(value.timestamp())
        except Exception:
            return None
    return None


def velocity_per_hour(messages: list[dict]) -> float:
    """Messages per hour across the sample's time span.

    Falls back to the raw count when dates are missing or unparseable.
    """
    count = len(messages)
    if count <= 1:
        return float(count)
    epochs = [e for e in (_to_epoch(m.get("date")) for m in messages) if e is not None]
    if len(epochs) < 2:
        return float(count)
    span_seconds = max(epochs) - min(epochs)
    if span_seconds <= 0:
        return float(count)
    hours = span_seconds / 3600.0
    if hours <= 0:
        return float(count)
    return count / hours


def pattern_hits(messages: list[dict], patterns: list[str]) -> dict:
    """Count regex matches per pattern across all message texts (non-zero only)."""
    hits: dict[str, int] = {}
    for pattern in patterns:
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            continue
        total = 0
        for m in messages:
            total += len(regex.findall(m.get("text") or ""))
        if total:
            hits[pattern] = total
    return hits


# ==========================================
# 4. SCORING
# ==========================================
def score_sample(features: dict) -> tuple[str, float, list[str]]:
    """Apply ordered rules; return (label, score, reasons)."""
    scam_hits = int(features.get("scam_hits", 0) or 0)
    spam_hits = int(features.get("spam_hits", 0) or 0)
    link_density_value = float(features.get("link_density", 0.0) or 0.0)
    duplicate_ratio_value = float(features.get("duplicate_ratio", 0.0) or 0.0)
    text_ratio_value = float(features.get("text_ratio", 0.0) or 0.0)
    velocity = float(features.get("velocity", 0.0) or 0.0)
    message_count = int(features.get("message_count", 0) or 0)

    # Cannot judge an empty sample: fall back to legacy heuristics instead of
    # condemning the chat (UNKNOWN must never block indexing).
    if message_count == 0:
        return UNKNOWN, 0.0, ["no-sample"]

    # 1. SCAM
    if scam_hits >= 2 or (scam_hits >= 1 and link_density_value > 0.30):
        reasons = [
            f"scam_hits={scam_hits}",
            f"link_density={link_density_value:.2f}",
        ]
        return SCAM, 0.9, reasons

    # 2. SPAM
    if (
        spam_hits >= 3
        or (link_density_value > 0.60 and duplicate_ratio_value > 0.40)
        or (velocity > 120 and duplicate_ratio_value > 0.50)
    ):
        reasons = [
            f"spam_hits={spam_hits}",
            f"link_density={link_density_value:.2f}",
            f"duplicate_ratio={duplicate_ratio_value:.2f}",
            f"velocity={velocity:.1f}",
        ]
        return SPAM, 0.7, reasons

    # 3. JUNK
    if duplicate_ratio_value > 0.70 or text_ratio_value < 0.20:
        reasons = [
            f"duplicate_ratio={duplicate_ratio_value:.2f}",
            f"text_ratio={text_ratio_value:.2f}",
        ]
        return JUNK, 0.6, reasons

    # 4. GOOD
    score = max(0.5, 1.0 - 0.1 * spam_hits - 0.5 * link_density_value)
    reasons = [
        f"spam_hits={spam_hits}",
        f"link_density={link_density_value:.2f}",
        f"text_ratio={text_ratio_value:.2f}",
    ]
    return GOOD, score, reasons


def label_allows_indexing(label: str) -> bool:
    """Only GOOD is indexed; UNKNOWN falls back to legacy heuristics."""
    return label == GOOD


# ==========================================
# 5. CLASSIFIER + OPTIONAL LLM HOOK
# ==========================================
def _build_llm_prompt(sample: dict, features: dict) -> str:
    """Compact JSON summary for the optional LLM classifier."""
    texts = [
        (m.get("text") or "")[:200]
        for m in sample.get("messages", [])[:15]
        if (m.get("text") or "").strip()
    ]
    payload = {
        "title": sample.get("title", ""),
        "username": sample.get("username", ""),
        "member_count": sample.get("member_count"),
        "features": features,
        "messages": texts,
    }
    return json.dumps(payload, ensure_ascii=False)


def _parse_llm_verdict(raw: Any) -> Optional[dict]:
    """Parse an LLM verdict from str or dict; tolerate markdown-fenced JSON."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    # Extract the first {...} block if the response is fenced or chatty.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def classify_chat(client, gate: FloodGate, chat, llm=None) -> dict:
    """Classify a chat as GOOD/SPAM/SCAM/JUNK. Never raises."""
    chat_id = getattr(chat, "id", 0)
    title = getattr(chat, "title", "") or ""
    checked_at = time.time()

    try:
        sample = await gather_chat_sample(client, gate, chat)
    except Exception as e:
        metrics.inc("quality_checks")
        metrics.inc("quality_unknown")
        return {
            "chat_id": chat_id,
            "title": title,
            "label": UNKNOWN,
            "score": 0.0,
            "reasons": [f"classify-error:{e}"],
            "sampled_messages": 0,
            "checked_at": checked_at,
        }

    messages = sample.get("messages", [])
    scam_hits_map = pattern_hits(messages, SCAM_PATTERNS)
    spam_hits_map = pattern_hits(messages, SPAM_PATTERNS)

    features = {
        "scam_hits": sum(scam_hits_map.values()),
        "spam_hits": sum(spam_hits_map.values()),
        "link_density": link_density(messages),
        "duplicate_ratio": duplicate_ratio(messages),
        "forward_ratio": forward_ratio(messages),
        "text_ratio": text_ratio(messages),
        "velocity": velocity_per_hour(messages),
        "member_count": sample.get("member_count"),
        "message_count": len(messages),
    }

    label, score, reasons = score_sample(features)

    # --- Optional LLM override ---
    if llm is not None and config.QUALITY_LLM_ENABLED:
        try:
            prompt = _build_llm_prompt(sample, features)
            raw = await llm(prompt)
            verdict = _parse_llm_verdict(raw)
            if verdict is not None:
                llm_label = str(verdict.get("label", "")).upper()
                try:
                    confidence = float(verdict.get("confidence", 0.0))
                except (TypeError, ValueError):
                    confidence = 0.0
                if confidence >= 0.8 and llm_label in {GOOD, SPAM, SCAM, JUNK}:
                    label = llm_label
                    score = confidence
                    reasons = list(reasons) + [f"llm:{verdict.get('reason', '')}"]
        except Exception:
            reasons = list(reasons) + ["llm-error"]

    metrics.inc("quality_checks")
    metrics.inc(f"quality_{label.lower()}")

    return {
        "chat_id": chat_id,
        "title": title,
        "label": label,
        "score": float(score),
        "reasons": reasons,
        "sampled_messages": len(messages),
        "checked_at": checked_at,
    }


# ==========================================
# 6. CACHE
# ==========================================
class QualityCache:
    """JSON cache at config.QUALITY_CACHE_FILE: {str(chat_id): verdict-dict}."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._data: dict = {}
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        path = Path(config.QUALITY_CACHE_FILE)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = {}
        self._data = data
        self._loaded = True

    def get(self, chat_id: int) -> Optional[dict]:
        """Return cached verdict if within TTL, else None."""
        self.load()
        entry = self._data.get(str(chat_id))
        if not isinstance(entry, dict):
            return None
        checked_at = entry.get("checked_at", 0)
        try:
            age = time.time() - float(checked_at)
        except (TypeError, ValueError):
            return None
        if age > QUALITY_CACHE_TTL_SECONDS:
            return None
        return entry

    async def set(self, chat_id: int, verdict: dict) -> None:
        async with self._lock:
            self.load()
            self._data[str(chat_id)] = verdict
            await asyncio.to_thread(self._write_atomic)

    def _write_atomic(self) -> None:
        path = Path(config.QUALITY_CACHE_FILE)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
