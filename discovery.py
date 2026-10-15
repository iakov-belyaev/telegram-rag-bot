"""Autonomous Cyprus group discovery: queue, adapters, join/mute/triage agent.

Sources feed a persistent DiscoveryQueue (seeds.yaml, Cyprus IT chat links,
@kiprinfobot menus, optional web search). The GroupDiscoveryAgent joins new
groups, mutes them immediately, quality-classifies them, and records verdicts
so only GOOD groups reach the OSINT index.
"""

import asyncio
import json
import os
import random
import re
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml
from pyrogram import errors as tg_errors

import config
import database as db
import metrics
from logging_config import get_logger
from quality import (
    GOOD,
    UNKNOWN,
    QualityCache,
    classify_chat,
    label_allows_indexing,
)
from scraper import (
    FloodGate,
    RateGate,
    TargetManager,
    iterate_with_flood,
    mute_chat,
    telegram_call,
)


_logger = get_logger("discovery")


def log(message: str) -> None:
    """Runtime log line: console line + JSON file via structured logging."""
    _logger.info("%s", message)


# ==========================================
# 1. PEER NORMALIZATION + LINK EXTRACTION
# ==========================================
RESERVED_TME_PATHS = {
    "joinchat", "addstickers", "proxy", "socks", "share", "iv", "setlanguage",
    "addtheme", "boost", "contact", "c", "k", "s", "login", "confirmphone",
}

_TME_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?t\.me/([A-Za-z0-9_+\-/]+)", re.IGNORECASE
)
_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_NUMERIC_RE = re.compile(r"^-?\d+$")

# Cap on how many links a single scan adapter will enqueue.
_SCAN_ENQUEUE_CAP = 200


def _bot_username() -> str:
    """Lowercased bot username (sans @) from config, or ''."""
    raw = getattr(config, "BOT_USERNAME", "") or ""
    return raw.lstrip("@").lower()


def normalize_peer(raw: str) -> Optional[str]:
    """Return canonical peer: "@name" | "https://t.me/+hash" | "https://t.me/joinchat/hash" | "-100…int".

    Accepts @name, t.me/name, https://t.me/name, t.me/+hash, t.me/joinchat/hash, numeric ids.
    Returns None for reserved/invalid targets.
    """
    if not raw:
        return None
    text = str(raw).strip().strip(".,;:!?)\"'<>")
    if not text:
        return None

    # Strip scheme/host prefixes down to a path-ish token.
    lowered = text.lower()
    for prefix in ("https://t.me/", "http://t.me/", "https://www.t.me/",
                   "http://www.t.me/", "t.me/", "www.t.me/"):
        if lowered.startswith(prefix):
            text = text[len(prefix):]
            break

    text = text.strip().strip("/")
    if not text:
        return None

    # Invite links: +hash or joinchat/hash
    if text.startswith("+"):
        token = text[1:].strip()
        if token and _NAME_RE.match(token):
            return f"https://t.me/+{token}"
        return None
    if text.lower().startswith("joinchat/"):
        token = text[len("joinchat/"):].strip()
        if token and _NAME_RE.match(token):
            return f"https://t.me/joinchat/{token}"
        return None

    # Numeric ids (optionally negative, e.g. -100…).
    if _NUMERIC_RE.match(text):
        digits = text.lstrip("-")
        if len(digits) >= 6:
            return text
        return None

    # Plain username.
    name = text.lstrip("@")
    if len(name) >= 4 and _NAME_RE.match(name):
        if name.lower() in RESERVED_TME_PATHS:
            return None
        if name.lower() == _bot_username():
            return None
        return f"@{name}"
    return None


def extract_tme_links(text: str) -> list[str]:
    """All canonicalized t.me targets in a text blob (regex n.N. t.me/...), deduped, order kept."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _TME_LINK_RE.finditer(text):
        peer = normalize_peer(match.group(0))
        if peer and peer not in seen:
            seen.add(peer)
            out.append(peer)
    return out


# ==========================================
# 2. DISCOVERY QUEUE (PERSISTENT, ATOMIC)
# ==========================================
def _atomic_write_json(path: str, data: Any) -> None:
    """Write JSON to a temp file then os.replace atomically."""
    config.ensure_state_dir()
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _load_json(path: str, default: Any) -> Any:
    """Load JSON from path; return default on missing/corrupt file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DiscoveryQueue:
    """Persistent queue of discovered peers awaiting triage.

    File: config.DISCOVERY_QUEUE_FILE.
    Shape: {"seen": {peer: iso}, "pending": [{"peer", "source", "added_at",
    "attempts", "meta"}], "enqueued_total": int}
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._data: dict = {"seen": {}, "pending": [], "enqueued_total": 0}
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        data = _load_json(config.DISCOVERY_QUEUE_FILE, None)
        if not isinstance(data, dict):
            data = {"seen": {}, "pending": [], "enqueued_total": 0}
        data.setdefault("seen", {})
        data.setdefault("pending", [])
        data.setdefault("enqueued_total", 0)
        if not isinstance(data["seen"], dict):
            data["seen"] = {}
        if not isinstance(data["pending"], list):
            data["pending"] = []
        self._data = data
        self._loaded = True

    def _find(self, peer: str) -> Optional[dict]:
        for item in self._data["pending"]:
            if item.get("peer") == peer:
                return item
        return None

    async def enqueue(self, peer: str, source: str, meta: Optional[dict] = None) -> bool:
        """Add a peer to the queue; return False if already seen."""
        if not peer:
            return False
        async with self._lock:
            self.load()
            if peer in self._data["seen"]:
                return False
            self._data["seen"][peer] = _now_iso()
            self._data["pending"].append({
                "peer": peer,
                "source": source,
                "added_at": _now_iso(),
                "attempts": 0,
                "meta": dict(meta or {}),
            })
            self._data["enqueued_total"] = self._data.get("enqueued_total", 0) + 1
            await asyncio.to_thread(
                _atomic_write_json, config.DISCOVERY_QUEUE_FILE, self._data
            )
            return True

    def pending(self, limit: Optional[int] = None) -> list[dict]:
        self.load()
        items = list(self._data["pending"])
        if limit is not None:
            return items[:limit]
        return items

    async def mark_done(self, peer: str) -> None:
        """Drop from pending, keep in seen."""
        async with self._lock:
            self.load()
            self._data["pending"] = [
                item for item in self._data["pending"] if item.get("peer") != peer
            ]
            self._data["seen"].setdefault(peer, _now_iso())
            await asyncio.to_thread(
                _atomic_write_json, config.DISCOVERY_QUEUE_FILE, self._data
            )

    async def note_attempt(self, peer: str) -> int:
        """Increment attempts for a peer; return the new count."""
        async with self._lock:
            self.load()
            item = self._find(peer)
            if item is None:
                return 0
            item["attempts"] = int(item.get("attempts", 0)) + 1
            await asyncio.to_thread(
                _atomic_write_json, config.DISCOVERY_QUEUE_FILE, self._data
            )
            return item["attempts"]

    async def save(self) -> None:
        """Persist current state to disk atomically."""
        async with self._lock:
            self.load()
            await asyncio.to_thread(
                _atomic_write_json, config.DISCOVERY_QUEUE_FILE, self._data
            )

    def stats(self) -> dict:
        self.load()
        return {
            "seen": len(self._data["seen"]),
            "pending": len(self._data["pending"]),
            "enqueued_total": int(self._data.get("enqueued_total", 0)),
        }


# ==========================================
# 3. ADAPTERS
# ==========================================
@dataclass
class DiscoveryContext:
    queue: DiscoveryQueue
    client: Any
    gate: FloodGate
    rate: RateGate
    seeds: dict = field(default_factory=dict)


# --- 3a. SeedAdapter ---
def load_seeds(path: Optional[str] = None) -> dict:
    """Read YAML seeds; missing/invalid → {"peers": [], "queries": []}."""
    target = path or config.DISCOVERY_SEEDS_FILE
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError("seeds root is not a mapping")
        peers = data.get("peers") or []
        queries = data.get("queries") or []
        if not isinstance(peers, list):
            peers = []
        if not isinstance(queries, list):
            queries = []
        return {
            "peers": [str(p) for p in peers],
            "queries": [str(q) for q in queries],
            "directory_urls": [str(u) for u in (data.get("directory_urls") or [])],
        }
    except FileNotFoundError:
        log(f"ℹ️ seeds file not found: {target}")
        return {"peers": [], "queries": [], "directory_urls": []}
    except Exception as e:  # noqa: BLE001
        log(f"⚠️ seeds load failed ({target}): {e}")
        return {"peers": [], "queries": [], "directory_urls": []}


class SeedAdapter:
    """Enqueues peers listed in the seeds YAML."""

    name = "seed"

    async def discover(self, ctx: DiscoveryContext) -> int:
        count = 0
        try:
            for raw in ctx.seeds.get("peers", []):
                peer = normalize_peer(raw)
                if not peer:
                    continue
                if await ctx.queue.enqueue(peer, "seed"):
                    count += 1
        except Exception as e:  # noqa: BLE001
            log(f"⚠️ SeedAdapter error: {e}")
        return count


# --- 3b. CyprusItAdapter ---
class CyprusItAdapter:
    """Scans already-indexed messages for t.me links."""

    name = "cyprus-it-chat"

    async def discover(self, ctx: DiscoveryContext) -> int:
        count = 0
        try:
            limit = int(getattr(config, "DISCOVERY_SCAN_MESSAGES_LIMIT", 500))
            target = (getattr(config, "CYPRUS_IT_CHAT", "") or "").strip()
            numeric_filter = bool(target) and _NUMERIC_RE.match(target.lstrip("-") or "")

            scroll_kwargs: dict = {
                "limit": limit,
                "with_payload": True,
                "with_vectors": False,
            }
            if numeric_filter:
                from qdrant_client.models import FieldCondition, Filter, MatchValue

                scroll_kwargs["scroll_filter"] = Filter(
                    must=[FieldCondition(key="chat_id", match=MatchValue(value=int(target)))]
                )

            points, _ = await db._qdrant_read(
                ([], None),
                db.get_qdrant().scroll,
                config.MESSAGES_COLLECTION,
                **scroll_kwargs,
            )

            scanned = 0
            for point in points or []:
                scanned += 1
                if scanned > limit:
                    break
                payload = getattr(point, "payload", None) or {}
                if target and not numeric_filter:
                    title = str(payload.get("chat_title", "") or "")
                    needle = target.lstrip("@").lower()
                    if needle and needle not in title.lower():
                        continue
                text = payload.get("text", "") or ""
                for peer in extract_tme_links(text):
                    if await ctx.queue.enqueue(peer, "cyprus-it-chat"):
                        count += 1
                    if count >= _SCAN_ENQUEUE_CAP:
                        return count
        except Exception as e:  # noqa: BLE001
            log(f"⚠️ CyprusItAdapter error: {e}")
        return count


# --- 3c. KiprinfobotAdapter ---
async def _send_and_collect(
    client, gate: FloodGate, target: str, text: str, timeout: int
) -> list[Any]:
    """Send `text` to target; poll history up to `timeout` s for newer messages from it.

    Returns list of message objects (newest-last). Never raises.
    """
    collected: list[Any] = []
    try:
        sent = await telegram_call(gate, client.send_message, target, text)
        sent_id = getattr(sent, "id", 0) or 0
        deadline = time.time() + max(1, int(timeout))
        while time.time() < deadline:
            await asyncio.sleep(2)
            try:
                async for msg in iterate_with_flood(
                    gate, client.get_chat_history, target, limit=20
                ):
                    mid = getattr(msg, "id", 0) or 0
                    if mid <= sent_id:
                        continue
                    if any(getattr(m, "id", None) == mid for m in collected):
                        continue
                    collected.append(msg)
            except Exception as e:  # noqa: BLE001
                log(f"⚠️ kiprinfobot poll error: {e}")
            if collected:
                break
    except Exception as e:  # noqa: BLE001
        log(f"⚠️ _send_and_collect failed for {target}: {e}")
    return collected


def _links_from_message(message: Any) -> list[str]:
    """Extract t.me links from a message's text and inline keyboard URLs."""
    links: list[str] = []
    try:
        links.extend(extract_tme_links(getattr(message, "text", "") or ""))
    except Exception:  # noqa: BLE001
        pass
    try:
        markup = getattr(message, "reply_markup", None)
        keyboard = getattr(markup, "inline_keyboard", None) if markup else None
        if keyboard:
            for row in keyboard:
                for button in row:
                    url = getattr(button, "url", None)
                    if url:
                        links.extend(extract_tme_links(url))
    except Exception:  # noqa: BLE001
        pass
    # Dedup, keep order.
    seen: set[str] = set()
    out: list[str] = []
    for link in links:
        if link not in seen:
            seen.add(link)
            out.append(link)
    return out


class KiprinfobotAdapter:
    """Interactive info-bot querying (guarded, paced)."""

    name = "kiprinfobot"

    DEFAULT_QUERIES = ["кипр группы чаты", "cyprus telegram groups"]

    async def discover(self, ctx: DiscoveryContext) -> int:
        if not getattr(config, "KIPRINFBOT_ENABLED", False):
            log("ℹ️ KiprinfobotAdapter disabled (KIPRINFBOT_ENABLED=false)")
            metrics.inc("discovery_kiprinfobot_skipped")
            return 0
        if ctx.client is None:
            return 0

        count = 0
        target = getattr(config, "KIPRINFBOT_USERNAME", "") or ""
        if not target:
            log("⚠️ KiprinfobotAdapter: no KIPRINFBOT_USERNAME configured")
            return 0

        try:
            await _send_and_collect(
                ctx.client,
                ctx.gate,
                target,
                "/start",
                int(getattr(config, "KIPRINFBOT_REPLY_TIMEOUT_SECONDS", 20)),
            )
            await asyncio.sleep(float(getattr(config, "KIPRINFBOT_DELAY_SECONDS", 3)))

            queries = list(ctx.seeds.get("queries", [])) + list(self.DEFAULT_QUERIES)
            max_queries = int(getattr(config, "KIPRINFBOT_MAX_QUERIES", 5))
            for query in queries[:max_queries]:
                messages = await _send_and_collect(
                    ctx.client,
                    ctx.gate,
                    target,
                    query,
                    int(getattr(config, "KIPRINFBOT_REPLY_TIMEOUT_SECONDS", 20)),
                )
                for msg in messages:
                    for peer in _links_from_message(msg):
                        if await ctx.queue.enqueue(peer, "kiprinfobot"):
                            count += 1
                await asyncio.sleep(float(getattr(config, "KIPRINFBOT_DELAY_SECONDS", 3)))
        except Exception as e:  # noqa: BLE001
            log(f"⚠️ KiprinfobotAdapter error: {e}")
        return count


# --- 3d. WebSearchAdapter ---
UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
]

DEFAULT_DIRECTORY_URLS: list[str] = []

_ROBOTS_CACHE: dict[str, bool] = {}


def _ua() -> str:
    return random.choice(UA_POOL)


async def _robots_allows(url: str) -> bool:
    """Check robots.txt for the URL's host; conservative on failure."""
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc
        if not host:
            return False
        if host in _ROBOTS_CACHE:
            return _ROBOTS_CACHE[host]
        robots_url = f"{parsed.scheme}://{host}/robots.txt"
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": _ua()}) as client:
            resp = await client.get(robots_url)
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(resp.text.splitlines())
        allowed = parser.can_fetch("*", url)
        _ROBOTS_CACHE[host] = allowed
        return allowed
    except Exception as e:  # noqa: BLE001
        log(f"⚠️ robots.txt fetch failed for {url}: {e}")
        metrics.inc("discovery_robots_failures")
        return False


class WebProvider:
    """Base class for pluggable web search providers."""

    name: str = "provider"

    async def search(self, query: str) -> list[str]:
        raise NotImplementedError


class DuckDuckGoHtmlProvider(WebProvider):
    """DuckDuckGo HTML endpoint scraper."""

    name = "ddg-html"

    async def search(self, query: str) -> list[str]:
        urls: list[str] = []
        try:
            endpoint = "https://html.duckduckgo.com/html/"
            if not await _robots_allows(endpoint):
                log("⚠️ robots.txt disallows DuckDuckGo HTML endpoint")
                return []
            await asyncio.sleep(float(getattr(config, "WEB_FETCH_DELAY_SECONDS", 2)))
            async with httpx.AsyncClient(
                timeout=15, headers={"User-Agent": _ua()}, follow_redirects=True
            ) as client:
                resp = await client.get(endpoint, params={"q": query})
            html = resp.text or ""
            for match in re.finditer(r'href="([^"]*uddg=[^"&]+[^"]*)"', html):
                raw = match.group(1)
                try:
                    parsed = urllib.parse.urlparse(raw)
                    qs = urllib.parse.parse_qs(parsed.query)
                    target = qs.get("uddg", [""])[0]
                    if target:
                        urls.append(urllib.parse.unquote(target))
                except Exception:  # noqa: BLE001
                    continue
        except Exception as e:  # noqa: BLE001
            log(f"⚠️ DuckDuckGoHtmlProvider error: {e}")
        return urls


class SitemapDirectoryProvider(WebProvider):
    """Fetches configured directory pages and extracts t.me links."""

    name = "directory"

    async def search(self, query: str) -> list[str]:
        urls: list[str] = []
        try:
            pages = list(DEFAULT_DIRECTORY_URLS)
            for page in pages:
                if not await _robots_allows(page):
                    log(f"⚠️ robots.txt disallows directory page: {page}")
                    continue
                await asyncio.sleep(float(getattr(config, "WEB_FETCH_DELAY_SECONDS", 2)))
                async with httpx.AsyncClient(
                    timeout=15, headers={"User-Agent": _ua()}, follow_redirects=True
                ) as client:
                    resp = await client.get(page)
                urls.extend(extract_tme_links(resp.text or ""))
        except Exception as e:  # noqa: BLE001
            log(f"⚠️ SitemapDirectoryProvider error: {e}")
        return urls


class WebSearchAdapter:
    """Runs pluggable web providers to discover t.me links."""

    name = "web"

    DEFAULT_QUERIES = [
        "site:t.me cyprus",
        "site:t.me cyprus group",
        "cyprus telegram chat join",
    ]

    def __init__(self, providers: Optional[list[WebProvider]] = None) -> None:
        self.providers = providers or [DuckDuckGoHtmlProvider(), SitemapDirectoryProvider()]

    async def discover(self, ctx: DiscoveryContext) -> int:
        if not getattr(config, "WEB_DISCOVERY_ENABLED", False):
            log("ℹ️ WebSearchAdapter disabled (WEB_DISCOVERY_ENABLED=false)")
            return 0

        count = 0
        try:
            queries = list(self.DEFAULT_QUERIES) + list(ctx.seeds.get("queries", []))[:3]
            cap = int(getattr(config, "WEB_MAX_RESULTS_PER_QUERY", 10))
            for provider in self.providers:
                for query in queries:
                    try:
                        results = await provider.search(query)
                    except Exception as e:  # noqa: BLE001
                        log(f"⚠️ provider {provider.name} failed: {e}")
                        continue
                    for url in results[:cap]:
                        for peer in extract_tme_links(url):
                            if await ctx.queue.enqueue(peer, f"web:{provider.name}"):
                                count += 1
        except Exception as e:  # noqa: BLE001
            log(f"⚠️ WebSearchAdapter error: {e}")
        return count


# ==========================================
# 4. GROUP DISCOVERY AGENT
# ==========================================
_PERMANENT_JOIN_ERRORS = (
    tg_errors.UsernameNotOccupied,
    tg_errors.UsernameInvalid,
    tg_errors.InviteHashExpired,
    tg_errors.InviteHashInvalid,
    tg_errors.ChannelPrivate,
)


class _FallbackChat:
    """Minimal chat snapshot used when get_chat fails."""

    def __init__(self, peer: str) -> None:
        self.id = peer
        self.title = peer
        self.username = None
        self.type = None


class GroupDiscoveryAgent:
    """Joins, mutes, triages and records discovered groups."""

    def __init__(
        self,
        client,
        gate: FloodGate,
        rate: RateGate,
        queue: DiscoveryQueue,
        cache: QualityCache,
        targets: TargetManager,
        control,
    ) -> None:
        self.client = client
        self.gate = gate
        self.rate = rate
        self.queue = queue
        self.cache = cache
        self.targets = targets
        self.control = control

    async def process_candidate(self, item: dict) -> dict:
        """join → mute → triage → record. Returns {"peer","state","label","score"}."""
        peer = item.get("peer", "")
        result = {"peer": peer, "state": "unknown", "label": UNKNOWN, "score": 0.0}
        if not peer:
            return result

        try:
            if int(item.get("attempts", 0)) >= 3:
                await self.queue.mark_done(peer)
                result["state"] = "dropped"
                return result

            joined_chat = None
            joined = False
            if getattr(config, "DISCOVERY_AUTO_JOIN", False):
                try:
                    joined_chat = await telegram_call(self.gate, self.client.join_chat, peer)
                    joined = True
                    metrics.inc("groups_joined")
                except tg_errors.UserAlreadyParticipant:
                    joined = True
                except _PERMANENT_JOIN_ERRORS as e:
                    log(f"⏭️ join rejected for {peer}: {e}")
                    metrics.inc("groups_join_rejected")
                    await self.queue.mark_done(peer)
                    result["state"] = "join_error"
                    return result
                except Exception as e:  # noqa: BLE001
                    log(f"⚠️ join error for {peer}: {e}")
                    metrics.inc("groups_join_errors")
                    await self.queue.note_attempt(peer)
                    result["state"] = "retry"
                    return result

            chat_id = getattr(joined_chat, "id", None) or peer

            if getattr(config, "DISCOVERY_MUTE_JOINED", False):
                try:
                    await mute_chat(self.client, chat_id)
                except Exception as e:  # noqa: BLE001
                    log(f"⚠️ mute failed for {peer}: {e}")
                    metrics.inc("mute_failures")

            chat = None
            try:
                chat = await telegram_call(self.gate, self.client.get_chat, peer)
            except Exception as e:  # noqa: BLE001
                log(f"⚠️ get_chat failed for {peer}: {e}")
                chat = _FallbackChat(peer)

            verdict = await classify_chat(self.client, self.gate, chat)
            if not isinstance(verdict, dict):
                verdict = {"label": UNKNOWN, "score": 0.0, "title": getattr(chat, "title", "")}
            label = verdict.get("label", UNKNOWN)
            score = float(verdict.get("score", 0.0) or 0.0)
            title = verdict.get("title") or getattr(chat, "title", "") or ""

            try:
                await self.cache.set(chat_id, verdict)
            except Exception as e:  # noqa: BLE001
                log(f"⚠️ cache.set failed for {peer}: {e}")

            try:
                await self.targets.set_state(
                    peer,
                    "joined",
                    title=title,
                    note=f"discovered:{item.get('source', '')}:{label}",
                )
            except Exception as e:  # noqa: BLE001
                log(f"⚠️ targets.set_state failed for {peer}: {e}")

            metrics.inc("groups_triaged")

            if label != GOOD:
                metrics.inc("groups_rejected")
                if getattr(config, "DISCOVERY_AUTO_LEAVE_BAD", False):
                    try:
                        await telegram_call(self.gate, self.client.leave_chat, chat_id)
                        metrics.inc("groups_left_bad")
                    except Exception as e:  # noqa: BLE001
                        log(f"⚠️ leave_chat failed for {peer}: {e}")

            await self.queue.mark_done(peer)
            result.update({"state": "joined" if joined else "triaged", "label": label, "score": score})
            return result
        except Exception as e:  # noqa: BLE001
            log(f"⚠️ process_candidate failed for {peer}: {e}")
            result["state"] = "error"
            return result

    async def run_cycle(self, seeds: Optional[dict] = None) -> dict:
        """Adapters → pending triage (max DISCOVERY_MAX_JOINS_PER_CYCLE)."""
        stats = {"enqueued": 0, "processed": 0, "joined": 0, "rejected": 0, "errors": 0}
        try:
            seeds = seeds if seeds is not None else load_seeds()
            ctx = DiscoveryContext(
                queue=self.queue,
                client=self.client,
                gate=self.gate,
                rate=self.rate,
                seeds=seeds,
            )

            adapters: list[Any] = [SeedAdapter(), CyprusItAdapter()]
            if self.client is not None:
                adapters.append(KiprinfobotAdapter())
            adapters.append(WebSearchAdapter())

            for adapter in adapters:
                try:
                    n = await adapter.discover(ctx)
                    stats["enqueued"] += int(n or 0)
                except Exception as e:  # noqa: BLE001
                    log(f"⚠️ adapter {getattr(adapter, 'name', '?')} error: {e}")
                    metrics.inc("discovery_adapter_errors")

            max_joins = int(getattr(config, "DISCOVERY_MAX_JOINS_PER_CYCLE", 5))
            for item in self.queue.pending(limit=max_joins):
                if getattr(self.control, "stop_requested", False):
                    break
                try:
                    await self.rate.acquire()
                except Exception:  # noqa: BLE001
                    pass
                result = await self.process_candidate(item)
                stats["processed"] += 1
                if result.get("state") in ("joined", "triaged"):
                    stats["joined"] += 1
                if result.get("label") not in (None, GOOD):
                    stats["rejected"] += 1
                if result.get("state") in ("error", "retry"):
                    stats["errors"] += 1
        except Exception as e:  # noqa: BLE001
            log(f"⚠️ run_cycle error: {e}")
        return stats

    async def discover_sweep(self, control) -> None:
        """Forever-loop honoring DISCOVERY_ENABLED/interval/pause/stop."""
        if not getattr(config, "DISCOVERY_ENABLED", False):
            log("ℹ️ discovery disabled (DISCOVERY_ENABLED=false) — sweep not started")
            return

        while not control.stop_requested:
            while getattr(control, "paused", False) and not control.stop_requested:
                await asyncio.sleep(5)
            if control.stop_requested:
                break

            try:
                stats = await self.run_cycle()
                log(f"🔎 discovery cycle: {stats}")
            except Exception as e:  # noqa: BLE001
                log(f"⚠️ discovery cycle error: {e}")

            interval = float(getattr(config, "DISCOVERY_INTERVAL_SECONDS", 3600))
            slept = 0.0
            while slept < interval and not control.stop_requested:
                if getattr(control, "paused", False):
                    break
                await asyncio.sleep(min(5.0, interval - slept))
                slept += 5.0


# ==========================================
# 5. WIRE-UP HELPER
# ==========================================
async def run_discovery(client, gate: FloodGate, rate: RateGate, control) -> None:
    """Top-level for main.py: builds queue/cache/targets/agent and runs discover_sweep."""
    try:
        queue = DiscoveryQueue()
        queue.load()

        cache = QualityCache()
        cache.load()

        targets = TargetManager()
        targets.load()

        agent = GroupDiscoveryAgent(client, gate, rate, queue, cache, targets, control)
        await agent.discover_sweep(control)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log(f"⚠️ run_discovery failed: {e}")
