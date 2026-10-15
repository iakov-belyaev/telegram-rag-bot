"""Parallel async ingestion engine (Pyrogram userbot side).

FloodWait-resilient multi-worker scraping, target-group joining, live listener,
paginated cursors, chat quality filter cache, and multi-modal text extraction.
"""

import asyncio
import io
import json
import os
import random
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

from pyrogram import errors as tg_errors
from pyrogram import filters
from qdrant_client.models import PointStruct

import config
import metrics
import storage as db
from logging_config import get_logger

# --- Multi-modal extraction (optional dependencies) ---
# Availability flags are set at import; warnings are logged lazily on first use.
try:
    import pytesseract
    from PIL import Image
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

try:
    from pypdf import PdfReader
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False

try:
    import docx  # python-docx
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False


_logger = get_logger("scraper")


def log(message: str) -> None:
    """Runtime log line: console line + JSON file via structured logging."""
    _logger.info("%s", message)


# ==========================================
# 1. FLOODGATE + TELEGRAM_CALL
# ==========================================
class FloodGate:
    """Global cooperative pause gate shared by all workers.

    When ANY worker hits a FloodWait, it trips the gate; EVERY worker waits at
    the gate before its next Telegram API call, preventing thundering-herd retries."""

    def __init__(self) -> None:
        self._resume_at: float = 0.0
        self._lock = asyncio.Lock()
        self._flood_events: int = 0

    async def trip(self, seconds: float) -> None:
        """Extend the pause: resume_at = max(resume_at, now + clamp(seconds*margin + jitter))."""
        async with self._lock:
            delay = seconds * config.FLOOD_WAIT_MARGIN + random.uniform(0, 3)
            delay = min(delay, config.FLOOD_WAIT_MAX_SLEEP_SECONDS)
            self._resume_at = max(self._resume_at, time.time() + delay)
            self._flood_events += 1
        metrics.inc("flood_wait_events")

    async def wait(self) -> None:
        """Block until the gate is clear (loop; safe under contention)."""
        while True:
            async with self._lock:
                remaining = self._resume_at - time.time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 1.0))

    @property
    def remaining(self) -> float:
        """Seconds until clear, 0.0 when clear."""
        return max(0.0, self._resume_at - time.time())

    @property
    def flood_events(self) -> int:
        return self._flood_events


async def telegram_call(gate: FloodGate, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Await gate, then call async function fn with FloodWait retry loop.

    - up to config.FLOOD_MAX_RETRIES retries on tg_errors.FloodWait:
      await gate.trip(float(e.value)); await gate.wait(); retry.
    - re-raise the FloodWait after the final retry.
    - other exceptions propagate to the caller unchanged.
    """
    await gate.wait()
    attempt = 0
    while True:
        try:
            return await fn(*args, **kwargs)
        except tg_errors.FloodWait as e:
            if attempt >= config.FLOOD_MAX_RETRIES:
                raise
            attempt += 1
            await gate.trip(float(e.value))
            await gate.wait()


async def iterate_with_flood(gate: FloodGate, fn: Callable[..., Any], /, *args: Any, **kwargs: Any):
    """Async-iterate an async-generator function with gate + FloodWait retry.

    On FloodWait mid-iteration the underlying iterator is RESTARTED from scratch;
    this is safe because callers pass an explicit offset (resumable pagination)
    and re-processed items are filtered by the message-id dedup guard.
    """
    attempt = 0
    while True:
        await gate.wait()
        try:
            async for item in fn(*args, **kwargs):
                yield item
            return
        except tg_errors.FloodWait as e:
            if attempt >= config.FLOOD_MAX_RETRIES:
                raise
            attempt += 1
            await gate.trip(float(e.value))
            await gate.wait()


# ==========================================
# 2. RATEGATE (TOKEN BUCKET)
# ==========================================
class RateGate:
    """Async token bucket pacing Telegram API calls globally."""

    def __init__(self, rate_per_second: float, burst: int) -> None:
        self._rate = float(rate_per_second)
        self._burst = int(burst)
        self._tokens = float(burst)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._updated
                self._updated = now
                self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = 1.0 - self._tokens
                wait_for = deficit / self._rate if self._rate > 0 else 1.0
            await asyncio.sleep(wait_for)


# ==========================================
# 3. SCRAPERCONTROL + SINGLETON
# ==========================================
class ScraperControl:
    """Shared status/control object for scraper internals, bot.py and api.py."""

    def __init__(self) -> None:
        self.paused: bool = False
        self.pause_reason: str = ""
        self.stop_requested: bool = False
        self.started_at: float = time.time()
        self.counters: dict = {"messages_indexed": 0, "chats_completed": 0}
        self.current_tasks: dict = {}      # {worker_name: chat_title}
        self.queue_depth: int = 0
        self.last_pass_started: str = ""
        self.last_pass_finished: str = ""
        self.last_error: str = ""
        self.gate: Optional["FloodGate"] = None   # set by run_scraper

    def pause(self, reason: str) -> None:
        self.paused = True
        self.pause_reason = reason

    def resume(self) -> None:
        self.paused = False
        self.pause_reason = ""

    def request_stop(self) -> None:
        self.stop_requested = True

    def note_indexed(self, n: int) -> None:
        self.counters["messages_indexed"] = self.counters.get("messages_indexed", 0) + n

    def set_current(self, worker: str, title: Optional[str]) -> None:
        if title is None:
            self.current_tasks.pop(worker, None)
        else:
            self.current_tasks[worker] = title

    def status(self) -> dict:
        """JSON-safe snapshot of scraper state."""
        gate = self.gate
        return {
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "stop_requested": self.stop_requested,
            "uptime_seconds": time.time() - self.started_at,
            "counters": dict(self.counters),
            "workers_active": len(self.current_tasks),
            "current_tasks": dict(self.current_tasks),
            "queue_depth": self.queue_depth,
            "last_pass_started": self.last_pass_started,
            "last_pass_finished": self.last_pass_finished,
            "last_error": self.last_error,
            "gate_remaining_seconds": gate.remaining if gate else 0.0,
            "flood_events_total": gate.flood_events if gate else 0,
        }


_CONTROL = ScraperControl()


def get_control() -> ScraperControl:
    """Return the module-level ScraperControl singleton."""
    return _CONTROL


# ==========================================
# 4. JSON STATE STORES (ATOMIC WRITES)
# ==========================================
def _atomic_write_json(path: "str | Path", data: Any) -> None:
    """Write JSON to a temp file then os.replace atomically."""
    config.ensure_state_dir()
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _load_json(path: "str | Path", default: Any) -> Any:
    """Load JSON from path; return default on missing/corrupt file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


class CursorStore:
    """Per-chat pagination cursors. File: config.SCRAPER_STATE_FILE.

    Shape: {"chats": {str(chat_id): {"title": str, "offset_id": int, "completed": bool,
    "messages_indexed": int, "last_error": str|None, "updated_at": iso}}}"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._data: dict = {"chats": {}}
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        data = _load_json(config.SCRAPER_STATE_FILE, {"chats": {}})
        if not isinstance(data, dict) or "chats" not in data:
            data = {"chats": {}}
        self._data = data
        self._loaded = True

    def get(self, chat_id: int) -> Optional[dict]:
        self.load()
        return self._data["chats"].get(str(chat_id))

    async def update(self, chat_id: int, **fields: Any) -> None:
        """Create-or-update the chat entry; bumps updated_at; atomic save."""
        async with self._lock:
            self.load()
            key = str(chat_id)
            entry = self._data["chats"].get(key, {})
            entry.update(fields)
            entry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._data["chats"][key] = entry
            await asyncio.to_thread(_atomic_write_json, config.SCRAPER_STATE_FILE, self._data)

    async def save(self) -> None:
        """Persist current state to disk atomically."""
        async with self._lock:
            self.load()
            await asyncio.to_thread(_atomic_write_json, config.SCRAPER_STATE_FILE, self._data)


class ChatFilterCache:
    """Verdict cache. File: config.CHAT_FILTER_CACHE_FILE.

    Shape: {str(chat_id): {"verdict": bool, "reason": str, "checked_at": epoch_float}}"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._data: dict = {}
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        data = _load_json(config.CHAT_FILTER_CACHE_FILE, {})
        if not isinstance(data, dict):
            data = {}
        self._data = data
        self._loaded = True

    def get(self, chat_id: int) -> Optional[tuple[bool, str]]:
        """Return cached (verdict, reason) if within TTL, else None."""
        self.load()
        entry = self._data.get(str(chat_id))
        if not entry:
            return None
        checked_at = entry.get("checked_at", 0)
        if time.time() - checked_at > config.CHAT_FILTER_CACHE_TTL_SECONDS:
            return None
        return bool(entry.get("verdict")), entry.get("reason", "")

    async def set(self, chat_id: int, verdict: bool, reason: str) -> None:
        async with self._lock:
            self.load()
            self._data[str(chat_id)] = {
                "verdict": bool(verdict),
                "reason": reason,
                "checked_at": time.time(),
            }
            await asyncio.to_thread(_atomic_write_json, config.CHAT_FILTER_CACHE_FILE, self._data)

    async def save(self) -> None:
        """Persist current cache to disk atomically."""
        async with self._lock:
            self.load()
            await asyncio.to_thread(_atomic_write_json, config.CHAT_FILTER_CACHE_FILE, self._data)


class TargetManager:
    """Managed group join targets. File: config.TARGET_GROUPS_FILE.

    Shape: {"targets": [{"peer": str, "title": str, "state": "pending|joined|error",
    "note": str, "added_at": iso, "updated_at": iso}]}"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._data: dict = {"targets": []}
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        data = _load_json(config.TARGET_GROUPS_FILE, {"targets": []})
        if not isinstance(data, dict) or "targets" not in data:
            data = {"targets": []}
        self._data = data
        self._loaded = True

    def all(self) -> list[dict]:
        self.load()
        return list(self._data["targets"])

    def _find(self, peer: str) -> Optional[dict]:
        for t in self._data["targets"]:
            if t.get("peer") == peer:
                return t
        return None

    async def add(self, peer: str) -> bool:
        """Add a target; return False if peer already present."""
        async with self._lock:
            self.load()
            if self._find(peer) is not None:
                return False
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._data["targets"].append({
                "peer": peer,
                "title": "",
                "state": "pending",
                "note": "",
                "added_at": now,
                "updated_at": now,
            })
            await asyncio.to_thread(_atomic_write_json, config.TARGET_GROUPS_FILE, self._data)
            return True

    async def remove(self, peer: str) -> bool:
        """Remove a target; return False if not present."""
        async with self._lock:
            self.load()
            before = len(self._data["targets"])
            self._data["targets"] = [t for t in self._data["targets"] if t.get("peer") != peer]
            if len(self._data["targets"]) == before:
                return False
            await asyncio.to_thread(_atomic_write_json, config.TARGET_GROUPS_FILE, self._data)
            return True

    async def set_state(self, peer: str, state: str, title: str = "", note: str = "") -> None:
        async with self._lock:
            self.load()
            entry = self._find(peer)
            if entry is None:
                now = time.strftime("%Y-%m-%dT%H:%M:%S")
                entry = {
                    "peer": peer,
                    "title": title,
                    "state": state,
                    "note": note,
                    "added_at": now,
                    "updated_at": now,
                }
                self._data["targets"].append(entry)
            else:
                entry["state"] = state
                if title:
                    entry["title"] = title
                entry["note"] = note
                entry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            await asyncio.to_thread(_atomic_write_json, config.TARGET_GROUPS_FILE, self._data)

    def summary(self) -> dict:
        self.load()
        counts = {"joined": 0, "pending": 0, "error": 0, "total": len(self._data["targets"])}
        for t in self._data["targets"]:
            state = t.get("state")
            if state in counts:
                counts[state] += 1
        return counts

    async def save(self) -> None:
        """Persist current targets to disk atomically."""
        async with self._lock:
            self.load()
            await asyncio.to_thread(_atomic_write_json, config.TARGET_GROUPS_FILE, self._data)


async def join_converge(client, gate: FloodGate, rate: RateGate, manager: TargetManager) -> dict:
    """Join every pending/error target with PARALLEL_JOIN_CONCURRENCY semaphore."""
    manager.load()
    pending = [t for t in manager.all() if t.get("state") in ("pending", "error")]
    sem = asyncio.Semaphore(config.PARALLEL_JOIN_CONCURRENCY)
    result = {"joined": 0, "error": 0}

    async def _join_one(peer: str) -> None:
        async with sem:
            await rate.acquire()
            await gate.wait()
            try:
                await telegram_call(gate, client.join_chat, peer)
                chat = None
                title = ""
                try:
                    chat = await telegram_call(gate, client.get_chat, peer)
                    title = getattr(chat, "title", "") or ""
                except Exception:
                    pass
                if config.DISCOVERY_MUTE_JOINED:
                    await mute_chat(client, getattr(chat, "id", None) or peer)
                await manager.set_state(peer, "joined", title=title)
                metrics.inc("groups_joined")
                result["joined"] += 1
            except tg_errors.UserAlreadyParticipant:
                if config.DISCOVERY_MUTE_JOINED:
                    await mute_chat(client, peer)
                await manager.set_state(peer, "joined")
                metrics.inc("groups_joined")
                result["joined"] += 1
            except Exception as e:
                await manager.set_state(peer, "error", note=str(e)[:200])
                result["error"] += 1

    await asyncio.gather(*(_join_one(t["peer"]) for t in pending))
    return result


async def mute_chat(client, chat_id) -> bool:
    """Mute a chat forever (silent, no previews). Never raises; returns success.

    Uses the raw API because Pyrogram has no high-level mute helper:
    account.UpdateNotifySettings takes an InputNotifyPeer wrapper and an
    InputPeerNotifySettings with mute_until as a 32-bit int (max = 2038 epoch).
    """
    try:
        from pyrogram.raw import functions as raw_functions
        from pyrogram.raw import types as raw_types

        peer = await client.resolve_peer(chat_id)
        await client.invoke(
            raw_functions.account.UpdateNotifySettings(
                peer=raw_types.InputNotifyPeer(peer=peer),
                settings=raw_types.InputPeerNotifySettings(
                    show_previews=False, silent=True, mute_until=2147483647
                ),
            )
        )
        return True
    except Exception as e:  # noqa: BLE001
        log(f"⚠️ mute failed for {chat_id}: {e}")
        metrics.inc("mute_failures")
        return False


_QUALITY_CACHE = None


def _quality_cache_singleton():
    """Lazy singleton for the discovery quality cache (import kept local)."""
    global _QUALITY_CACHE
    if _QUALITY_CACHE is None:
        from quality import QualityCache

        _QUALITY_CACHE = QualityCache()
        _QUALITY_CACHE.load()
    return _QUALITY_CACHE


# ==========================================
# 5. CHAT QUALITY FILTER (CACHED)
# ==========================================
NOISE_TITLE_REGEX = re.compile("|".join(config.NOISE_TITLE_PATTERNS), re.IGNORECASE)


async def is_high_value_chat(client, gate: FloodGate, cache: ChatFilterCache, chat) -> tuple[bool, str]:
    """Decide whether a chat is worth scraping; (verdict, reason).

    Quality-classifier verdicts (from discovery triage) take precedence: GOOD is
    indexed, SPAM/SCAM/JUNK are never indexed, UNKNOWN/missing falls through to
    the legacy heuristic filter below.
    """
    # 0. Quality classifier verdict (lazy import avoids a module cycle)
    try:
        from quality import GOOD

        qcache = _quality_cache_singleton()
        verdict = qcache.get(chat.id)
        if verdict is not None:
            label = verdict.get("label")
            if label == GOOD:
                return True, "quality:GOOD"
            if label:
                return False, f"quality:{label}"
    except Exception as e:  # noqa: BLE001
        log(f"⚠️ quality cache consult failed: {e}")

    # 1. Cache hit
    cached = cache.get(chat.id)
    if cached is not None:
        return cached

    # 2. Title / username keyword filter
    haystack = f"{chat.title or ''} {chat.username or ''}"
    match = NOISE_TITLE_REGEX.search(haystack)
    if match:
        reason = f"noise-title:{match.group(0)}"
        await cache.set(chat.id, False, reason)
        return False, reason

    # 3. Broadcast-only channels
    if str(chat.type) == "ChatType.CHANNEL":
        await cache.set(chat.id, False, "broadcast-channel")
        return False, "broadcast-channel"

    # 4. Sample recent history to compute text-to-media ratio
    try:
        total = 0
        text_bearing = 0
        async for message in iterate_with_flood(
            gate, client.get_chat_history, chat.id, limit=config.FILTER_SAMPLE_SIZE
        ):
            total += 1
            raw_text = message.text or message.caption or ""
            if len(raw_text.strip()) >= 10:
                text_bearing += 1

        if total >= config.FILTER_MIN_SAMPLES:
            ratio = text_bearing / total
            if ratio < config.MIN_TEXT_RATIO:
                reason = f"low-text-ratio:{ratio:.2f}"
                await cache.set(chat.id, False, reason)
                return False, reason
    except Exception as e:
        return False, f"filter-error:{e}"

    # 5. High value
    await cache.set(chat.id, True, "ok")
    return True, "ok"


# ==========================================
# 6. MULTI-MODAL EXTRACTION
# ==========================================
def _ocr_image_bytes(image_bytes: bytes) -> str:
    """Synchronous OCR on raw image bytes. Returns extracted text or ''."""
    if not OCR_AVAILABLE:
        log("⚠️ pytesseract/Pillow not installed — image OCR disabled.")
        return ""
    try:
        image: Any = Image.open(io.BytesIO(image_bytes))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        return pytesseract.image_to_string(image, lang=config.OCR_LANGUAGES).strip()
    except Exception as e:
        log(f"⚠️ OCR error: {e}")
        return ""


def _extract_pdf_text(path: str) -> str:
    """Synchronous PDF text extraction. Returns extracted text or ''."""
    if not PYPDF_AVAILABLE:
        log("⚠️ pypdf not installed — PDF extraction disabled.")
        return ""
    try:
        reader = PdfReader(path)
        chunks = []
        for page in reader.pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(chunks).strip()
    except Exception as e:
        log(f"⚠️ PDF extraction error: {e}")
        return ""


def _extract_docx_text(path: str) -> str:
    """Synchronous DOCX text extraction. Returns extracted text or ''."""
    if not DOCX_AVAILABLE:
        log("⚠️ python-docx not installed — DOCX extraction disabled.")
        return ""
    try:
        document = docx.Document(path)
        return "\n".join(p.text for p in document.paragraphs).strip()
    except Exception as e:
        log(f"⚠️ DOCX extraction error: {e}")
        return ""


def _extract_txt_text(path: str) -> str:
    """Synchronous plain-text extraction. Returns extracted text or ''."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except Exception as e:
        log(f"⚠️ TXT extraction error: {e}")
        return ""


async def extract_text_from_media(client, gate: FloodGate, message) -> tuple[str, str]:
    """Return (text, source_type) for photo/document messages; ("", "none") if nothing."""
    # --- Photo OCR ---
    if getattr(message, "photo", None):
        if not OCR_AVAILABLE:
            log("⚠️ pytesseract/Pillow not installed — image OCR disabled.")
            return "", "none"
        try:
            buffer = await telegram_call(gate, client.download_media, message, in_memory=True)
            if buffer is None:
                return "", "none"
            image_bytes = buffer.getvalue() if hasattr(buffer, "getvalue") else bytes(buffer)
            text = await asyncio.to_thread(_ocr_image_bytes, image_bytes)
            if len(text) >= config.MIN_EXTRACTED_TEXT_LENGTH:
                return text, "photo_ocr"
        except Exception as e:
            log(f"⚠️ Photo download/OCR failed: {e}")
        return "", "none"

    # --- Document extraction ---
    document = getattr(message, "document", None)
    if document:
        file_name = (document.file_name or "").lower()
        ext = os.path.splitext(file_name)[1]
        if ext not in config.SUPPORTED_DOC_EXTENSIONS:
            return "", "none"
        if (document.file_size or 0) > config.MAX_DOCUMENT_BYTES:
            log(f"⚠️ Skipping oversized document ({document.file_size} bytes): {file_name}")
            return "", "none"

        tmp_path = None
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=ext)
            os.close(tmp_fd)
            await telegram_call(gate, client.download_media, message, file_name=tmp_path)

            if ext == ".pdf":
                text = await asyncio.to_thread(_extract_pdf_text, tmp_path)
                source_type = "document_pdf"
            elif ext == ".docx":
                text = await asyncio.to_thread(_extract_docx_text, tmp_path)
                source_type = "document_docx"
            else:  # .txt
                text = await asyncio.to_thread(_extract_txt_text, tmp_path)
                source_type = "document_txt"

            if len(text) >= config.MIN_EXTRACTED_TEXT_LENGTH:
                return text, source_type
        except Exception as e:
            log(f"⚠️ Document extraction failed for {file_name}: {e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        return "", "none"

    return "", "none"

# ==========================================
# 7. BATCH INDEXING
# ==========================================
async def index_messages_batch(client, gate: FloodGate, chat, messages: list) -> int:
    """Extract text, dedup against the cluster, embed, and upsert messages.

    Returns the number of points upserted. Never raises for individual-message
    problems; returns 0 on unexpected errors after logging.
    """
    control = get_control()
    try:
        # 1. Build candidates (text or extracted media text).
        candidates: list[tuple[Any, str, str]] = []
        for m in messages:
            text = (m.text or m.caption or "").strip()
            source_type = "text"
            if len(text) < config.MIN_EXTRACTED_TEXT_LENGTH:
                text, source_type = await extract_text_from_media(client, gate, m)
                if not text:
                    continue
            candidates.append((m, text, source_type))

        if not candidates:
            return 0

        # 2. Dedup guard against already-indexed message ids.
        existing = await db.filter_existing_message_ids(
            chat.id, [m.id for m, _, _ in candidates]
        )
        candidates = [(m, t, s) for (m, t, s) in candidates if m.id not in existing]
        if not candidates:
            return 0

        # 3. Embed and build points.
        vectors = await db.embed_texts([t for _, t, _ in candidates])
        points = [
            PointStruct(
                id=db.message_point_id(chat.id, m.id),
                vector=v,
                payload=db.build_message_payload(chat, m, t, source_type=s),
            )
            for (m, t, s), v in zip(candidates, vectors, strict=True)
        ]

        # 3b. Watchlist scan — alert-only side channel; must never block ingest.
        try:
            import watchlist

            for point in points:
                await watchlist.check_payload(point.payload or {})
        except Exception as e:  # noqa: BLE001
            log(f"⚠️ watchlist scan failed: {e}")

        # 4. Ensure chat metadata exists, then upsert points.
        await db.upsert_chat(chat)
        n = await db.upsert_messages(points)
        control.note_indexed(n)
        metrics.inc("messages_scraped", n)
        return n
    except Exception as e:
        log(f"⚠️ index_messages_batch failed for chat {getattr(chat, 'id', '?')}: {e}")
        control.last_error = str(e)[:200]
        return 0


# ==========================================
# 8. PER-CHAT PAGINATION WORKER
# ==========================================
async def sync_chat_history(
    client,
    gate: FloodGate,
    rate: RateGate,
    cursors: CursorStore,
    chat,
    control: ScraperControl,
) -> int:
    """Paginate one chat's full history until done/paused/stopped. Returns indexed count."""
    entry = cursors.get(chat.id) or {}
    if entry.get("completed"):
        return 0

    offset_id = entry.get("offset_id", 0)
    already_indexed = entry.get("messages_indexed", 0) or 0
    total = 0

    try:
        while True:
            if control.stop_requested:
                break
            while control.paused:
                await asyncio.sleep(5)
                if control.stop_requested:
                    break

            await rate.acquire()

            page = []
            async for m in iterate_with_flood(
                gate,
                client.get_chat_history,
                chat.id,
                limit=config.SCRAPER_PAGE_SIZE,
                offset_id=offset_id,
            ):
                page.append(m)
                offset_id = m.id

            if not page:
                await cursors.update(chat.id, completed=True, offset_id=offset_id)
                control.counters["chats_completed"] = control.counters.get("chats_completed", 0) + 1
                break

            n = await index_messages_batch(client, gate, chat, page)
            total += n
            await cursors.update(
                chat.id,
                offset_id=offset_id,
                messages_indexed=already_indexed + total,
                last_error=None,
            )

            if len(page) < config.SCRAPER_PAGE_SIZE:
                await cursors.update(chat.id, completed=True, offset_id=offset_id)
                control.counters["chats_completed"] = control.counters.get("chats_completed", 0) + 1
                break

            await asyncio.sleep(config.SCRAPER_MIN_DELAY_SECONDS)
    except Exception as e:
        log(f"⚠️ sync_chat_history failed for chat {getattr(chat, 'id', '?')}: {e}")
        control.last_error = str(e)[:200]
        try:
            await cursors.update(chat.id, last_error=str(e)[:200])
        except Exception:
            pass
        return total

    return total


# ==========================================
# 9. SUPERVISOR (PARALLEL WORKERS OVER A QUEUE)
# ==========================================
async def scraper_supervisor(
    client,
    gate: FloodGate,
    rate: RateGate,
    cursors: CursorStore,
    cache: ChatFilterCache,
    targets: TargetManager,
    control: ScraperControl,
) -> None:
    """Forever: build a pass of chats to sync, run SCRAPER_WORKERS consumers, sleep, repeat."""
    while True:
        if control.stop_requested:
            return

        while control.paused:
            await asyncio.sleep(5)
            if control.stop_requested:
                return

        control.last_pass_started = time.strftime("%Y-%m-%dT%H:%M:%S")

        # 1. Best-effort join of pending targets.
        try:
            join_result = await join_converge(client, gate, rate, targets)
            log(f"🔗 join_converge: {join_result}")
        except Exception as e:
            log(f"⚠️ join_converge failed: {e}")
            control.last_error = str(e)[:200]

        # 2. Enumerate dialogs.
        dialogs = []
        try:
            async for d in iterate_with_flood(gate, client.get_dialogs):
                dialogs.append(d)
        except Exception as e:
            log(f"⚠️ get_dialogs failed: {e}")
            control.last_error = str(e)[:200]

        # 3. Filter to high-value, not-yet-completed chats.
        work_chats = []
        for d in dialogs:
            chat = d.chat
            entry = cursors.get(chat.id) or {}
            if entry.get("completed"):
                continue
            try:
                verdict, reason = await is_high_value_chat(client, gate, cache, chat)
            except Exception as e:
                log(f"⚠️ filter error for {getattr(chat, 'id', '?')}: {e}")
                continue
            if not verdict:
                log(f"⏭️ skip {getattr(chat, 'title', chat.id)} ({reason})")
                continue
            work_chats.append(chat)

        # 4. Build the work queue.
        queue: asyncio.Queue = asyncio.Queue()
        for chat in work_chats:
            queue.put_nowait(chat)
        control.queue_depth = queue.qsize()

        # 5. Spawn consumers.
        async def _consumer(worker_name: str, queue: asyncio.Queue = queue) -> None:
            # bind this cycle's queue explicitly: the closure is recreated per
            # scrape cycle and must never pick up a later cycle's queue.
            while not control.stop_requested:
                try:
                    chat = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                control.set_current(worker_name, chat.title or str(chat.id))
                try:
                    await sync_chat_history(client, gate, rate, cursors, chat, control)
                except Exception as e:
                    log(f"⚠️ worker {worker_name} error: {e}")
                    control.last_error = str(e)[:200]
                finally:
                    control.set_current(worker_name, None)
                    control.queue_depth = queue.qsize()
                    queue.task_done()

        consumers = [
            asyncio.create_task(_consumer(f"worker-{i}"))
            for i in range(config.SCRAPER_WORKERS)
        ]

        # 6/7. Wait for the queue to drain, then cancel stragglers.
        try:
            await queue.join()
        except asyncio.CancelledError:
            for t in consumers:
                t.cancel()
            raise
        finally:
            for t in consumers:
                t.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)
            control.queue_depth = 0

        # 8. Pass summary + idle.
        control.last_pass_finished = time.strftime("%Y-%m-%dT%H:%M:%S")
        log(
            f"✅ pass complete: {len(work_chats)} chats, "
            f"indexed={control.counters.get('messages_indexed', 0)}, "
            f"completed={control.counters.get('chats_completed', 0)}"
        )
        await asyncio.sleep(config.SCRAPER_IDLE_SLEEP_SECONDS)


# ==========================================
# 10. LIVE LISTENER (REAL-TIME INGESTION)
# ==========================================
def register_live_listener(
    client, gate: FloodGate, rate: RateGate, control: ScraperControl
) -> None:
    """Register the real-time handler on the userbot (text/caption/photo/document)."""

    @client.on_message(
        filters.text | filters.caption | filters.photo | filters.document
    )
    async def _live_handler(client, message) -> None:  # noqa: ARG001
        if control.stop_requested:
            return
        if control.paused:
            return
        try:
            await rate.acquire()
            await index_messages_batch(client, gate, message.chat, [message])
        except Exception as e:
            log(f"⚠️ live handler error: {e}")
            control.last_error = str(e)[:200]


# ==========================================
# 11. TOP-LEVEL RUNNER + EXPORTS
# ==========================================
async def run_scraper(client) -> None:
    """Entry point used by main.py."""
    config.ensure_state_dir()

    gate = FloodGate()
    rate = RateGate(config.TG_OPS_PER_SECOND, config.TG_OPS_BURST)

    control = get_control()
    control.gate = gate

    cursors = CursorStore()
    cursors.load()
    cache = ChatFilterCache()
    cache.load()
    targets = TargetManager()
    targets.load()

    register_live_listener(client, gate, rate, control)

    log(
        f"🚀 scraper starting: targets={targets.summary()}, "
        f"workers={config.SCRAPER_WORKERS}"
    )

    try:
        await scraper_supervisor(client, gate, rate, cursors, cache, targets, control)
    except asyncio.CancelledError:
        log("🛑 scraper cancelled — persisting state")
        await cursors.save()
        await cache.save()
        await targets.save()
        raise
    finally:
        try:
            await cursors.save()
            await cache.save()
            await targets.save()
        except Exception as e:
            log(f"⚠️ state save failed on shutdown: {e}")

    log("👋 scraper stopped")
