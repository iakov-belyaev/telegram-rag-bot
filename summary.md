# Codebase Audit — Telegram RAG Engine

## Overall Grade: **B−**

| Dimension | Rating |
|---|---|
| Architecture | **B** — Clean dual-client (userbot + inline bot) split, but heavy duplication across `start.py`, `listener.py`, `scraper.py`, `bot.py`. |
| Reliability | **C+** — FloodWait handled; no retry/backoff on Qdrant or DeepSeek; single-process, no crash recovery. |
| Scale/Performance | **B−** — Batched embeddings + threaded inference are good; per-message chat upsert is wasteful; no pagination cursor persistence. |
| Intelligence | **B+** — Strong RAG prompt, multi-modal OCR/PDF/DOCX, semantic chat directory. DeepSeek call is unguarded. |

## Strengths & Bottlenecks

- ✅ **Works:** Real-time ingestion, batched embeddings, OCR/PDF/DOCX extraction, StorageShield capacity guard, noise-title regex filter, semantic chat directory.
- ⚠️ **Bottleneck:** Massive code duplication — `build_message_link`, `embed_texts`, chat upsert, and payload construction are copy-pasted 4×.
- ⚠️ **Bottleneck:** `is_high_value_chat()` runs a 30-message history fetch on *every* dialog on *every* deep-scraper pass — O(N) Telegram API calls per loop.
- ⚠️ **Bottleneck:** No dedup guard — re-scraping re-embeds and re-upserts identical points (UUIDv5 helps, but embedding cost is wasted).
- ⚠️ **Bottleneck:** DeepSeek + Qdrant calls have no timeout/retry; a single hang stalls the inline query.

## Prioritized Action Items

1. **Extract shared module** (`core.py`): `build_message_link`, `embed_texts`, `upsert_chat`, `build_message_payload`, `qdrant`/`deepseek` clients. Eliminates ~400 LOC of duplication.
2. **Cache noise-filter verdicts** in a local SQLite/JSON keyed by `chat.id` with TTL — stop re-sampling history every pass.
3. **Add retry/timeout wrappers** around Qdrant upserts and DeepSeek calls (`tenacity` + `asyncio.wait_for`).
4. **Persist scraper cursor** (`offset_id` per chat) so restarts resume instead of re-walking from newest.
5. **Harden StorageShield**: use `points_count` (not `vectors_count`, deprecated in Qdrant ≥1.13), and gate *live* ingestion too — currently only background scrapers pause.

## File-by-File Status

- **`start.py`** — Unified engine; functional but bloated (~600 LOC). Needs extraction of shared helpers + cursor persistence.
- **`listener.py`** — Standalone listener; **redundant** with `start.py`. Delete or reduce to thin wrapper.
- **`scraper.py`** — One-shot historical scraper; **redundant** with `continuous_deep_scraper()`. Delete or reduce to thin wrapper.
- **`bot.py`** — Standalone inline bot; **redundant** with `start.py`. Delete or reduce to thin wrapper.
- **`setup_db.py`** — Clean. Add payload indexes for `sender_username` and `source_type`.
- **`start.sh`** — Fine. Add `set -euo pipefail` and log rotation hint.
- **`.gitignore`** — Good coverage; now also ignores `summary.md`.
- **`README.md`** — Accurate but missing multi-modal (OCR/PDF/DOCX) documentation.
