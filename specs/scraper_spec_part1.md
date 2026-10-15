# Spec PART 1 of 2: `scraper.py` — gates, control, state stores, filters, multimodal

This is pass 1 of 2 for `scraper.py`. Implement ONLY sections 1-6 below, replacing the
`# STUB_PART_1:` line. LEAVE the `# STUB_PART_2:` line exactly as is — pass 2 fills it.

## File preamble (part of this pass)

Put the FULL import block at the top when implementing this pass (union of both passes):

```python
import asyncio
import io
import json
import os
import random
import re
import tempfile
import time
from typing import Any, Callable, Optional

from pyrogram import errors as tg_errors
from pyrogram import filters
from qdrant_client.models import PointStruct

import config
import database as db
```

Plus the optional multi-modal block (try/except ImportError with availability flags
`OCR_AVAILABLE`, `PYPDF_AVAILABLE`, `DOCX_AVAILABLE`; warn via log() when missing —
do NOT print at import; store a flag and log lazily on first use).

And a runtime logger:

```python
def log(message: str) -> None:
    """Timestamped runtime log line (never called at import time)."""
    print(f"[scraper {time.strftime('%H:%M:%S')}] {message}", flush=True)
```
(import time in the block above.)

## 1. FloodGate + telegram_call (FloodWait resilience core)

```python
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

    async def wait(self) -> None:
        """Block until the gate is clear (loop; safe under contention)."""

    @property
    def remaining(self) -> float: ...   # seconds until clear, 0.0 when clear

    @property
    def flood_events(self) -> int: ...
```

Details: clamp `seconds * config.FLOOD_WAIT_MARGIN + random.uniform(0, 3)` to
`config.FLOOD_WAIT_MAX_SLEEP_SECONDS`; increment `_flood_events` on each trip;
`wait()` sleeps the remaining time in <=1s increments while under lock check.

```python
async def telegram_call(gate: FloodGate, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Await gate, then call async function fn with FloodWait retry loop.

    - up to config.FLOOD_MAX_RETRIES retries on tg_errors.FloodWait:
      await gate.trip(float(e.value)); await gate.wait(); retry.
    - re-raise the FloodWait after the final retry.
    - other exceptions propagate to the caller unchanged.
    """
```

## 2. RateGate (token bucket)

```python
class RateGate:
    """Async token bucket pacing Telegram API calls globally."""
    def __init__(self, rate_per_second: float, burst: int) -> None: ...
    async def acquire(self) -> None: ...
```
Standard token bucket: tokens refill at rate_per_second up to burst; acquire waits
(asyncio.sleep) for the next token if empty. No external deps.

## 3. ScraperControl + singleton

```python
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

    def pause(self, reason: str) -> None: ...
    def resume(self) -> None: ...
    def request_stop(self) -> None: ...
    def note_indexed(self, n: int) -> None: ...
    def set_current(self, worker: str, title: Optional[str]) -> None: ...  # None clears
    def status(self) -> dict:
        """JSON-safe snapshot: paused, pause_reason, stop_requested, uptime_seconds,
        counters, workers_active (len current_tasks), current_tasks, queue_depth,
        last_pass_started, last_pass_finished, last_error, gate_remaining_seconds,
        flood_events_total (from gate if set else 0)."""

_CONTROL = ScraperControl()

def get_control() -> ScraperControl:
    """Return the module-level ScraperControl singleton."""
```

## 4. JSON state stores (atomic writes)

All inherit nothing; each has `self._lock = asyncio.Lock()`, `load()` (sync, lazy, safe
on missing/corrupt file → defaults), `async save()` writing tmp file + `os.replace`
atomically. Paths come from config. Use `config.ensure_state_dir()` inside save().

```python
class CursorStore:
    """Per-chat pagination cursors. File: config.SCRAPER_STATE_FILE.
    Shape: {"chats": {str(chat_id): {"title": str, "offset_id": int, "completed": bool,
    "messages_indexed": int, "last_error": str|None, "updated_at": iso}}}"""
    def load(self) -> None: ...
    def get(self, chat_id: int) -> Optional[dict]: ...
    async def update(self, chat_id: int, **fields: Any) -> None:
        """Create-or-update the chat entry; bumps updated_at; atomic save."""

class ChatFilterCache:
    """Verdict cache. File: config.CHAT_FILTER_CACHE_FILE.
    Shape: {str(chat_id): {"verdict": bool, "reason": str, "checked_at": epoch_float}}"""
    def load(self) -> None: ...
    def get(self, chat_id: int) -> Optional[tuple[bool, str]]:
        """Return cached (verdict, reason) if within config.CHAT_FILTER_CACHE_TTL_SECONDS, else None."""
    async def set(self, chat_id: int, verdict: bool, reason: str) -> None: ...

class TargetManager:
    """Managed group join targets. File: config.TARGET_GROUPS_FILE.
    Shape: {"targets": [{"peer": str, "title": str, "state": "pending|joined|error",
    "note": str, "added_at": iso, "updated_at": iso}]}"""
    def load(self) -> None: ...
    def all(self) -> list[dict]: ...
    async def add(self, peer: str) -> bool:      # False if peer already present
    async def remove(self, peer: str) -> bool:   # False if not present
    async def set_state(self, peer: str, state: str, title: str = "", note: str = "") -> None
    def summary(self) -> dict:                    # {"joined": n, "pending": n, "error": n, "total": n}

async def join_converge(client, gate: FloodGate, rate: RateGate, manager: TargetManager) -> dict:
    """Join every pending/error target with config.PARALLEL_JOIN_CONCURRENCY semaphore.

    Per target: await rate.acquire(); await gate.wait(); then
    await telegram_call(gate, client.join_chat, peer).
    - Success → set_state(joined, title=<client.get_chat title best-effort>)
    - tg_errors.UserAlreadyParticipant → set_state(joined)
    - Any other Exception → set_state(error, note=str(e)[:200]); continue.
    Return {"joined": n, "error": n} counts of THIS run.
    """
```

## 5. Chat quality filter (cached)

```python
NOISE_TITLE_REGEX = re.compile("|".join(config.NOISE_TITLE_PATTERNS), re.IGNORECASE)

async def is_high_value_chat(client, gate: FloodGate, cache: ChatFilterCache, chat) -> tuple[bool, str]:
    """Decide whether a chat is worth scraping; (verdict, reason).

    1. Cache hit (cache.get) → return it (this avoids re-sampling every pass).
    2. Regex match on f"{chat.title or ''} {chat.username or ''}" → (False, f"noise-title:{match}") cached.
    3. str(chat.type) == "ChatType.CHANNEL" → (False, "broadcast-channel") cached.
    4. Sample up to config.FILTER_SAMPLE_SIZE recent messages via
       telegram_call(gate, client.get_chat_history, chat.id, limit=SAMPLE);
       if total >= config.FILTER_MIN_SAMPLES and text/caption ratio < config.MIN_TEXT_RATIO
       → (False, f"low-text-ratio:{ratio:.2f}") cached.
    5. Else (True, "ok") cached.
    On RPC/other exceptions → return (False, f"filter-error:{e}") WITHOUT caching.
    """
```

## 6. Multi-modal extraction (ported from v1 start.py, client-parameterized)

Sync helpers (same behavior as v1):
- `def _ocr_image_bytes(image_bytes: bytes) -> str` — PIL open; convert to RGB unless already
  RGB/L; `pytesseract.image_to_string(image, lang=config.OCR_LANGUAGES).strip()`; on ANY
  exception log and return "".
- `def _extract_pdf_text(path: str) -> str` — pypdf, join page texts, strip.
- `def _extract_docx_text(path: str) -> str` — python-docx paragraphs joined.
- `def _extract_txt_text(path: str) -> str` — utf-8 errors="ignore" read.

```python
async def extract_text_from_media(client, gate: FloodGate, message) -> tuple[str, str]:
    """Return (text, source_type) for photo/document messages; ("", "none") if nothing.

    Photo: client.download_media(message, in_memory=True) via telegram_call, get bytes via
    buffer.getvalue() if available else bytes(buffer); OCR in asyncio.to_thread; accept only if
    len(text) >= config.MIN_EXTRACTED_TEXT_LENGTH → ("photo_ocr").
    Document: only extensions in config.SUPPORTED_DOC_EXTENSIONS; skip if
    (document.file_size or 0) > config.MAX_DOCUMENT_BYTES; download via telegram_call to a
    tempfile.mkstemp(suffix=ext) path (close fd first); extract via asyncio.to_thread;
    ALWAYS remove temp file in finally; source_type document_pdf/document_docx/document_txt;
    accept only if len(text) >= config.MIN_EXTRACTED_TEXT_LENGTH, else ("", "none").
    """
```
