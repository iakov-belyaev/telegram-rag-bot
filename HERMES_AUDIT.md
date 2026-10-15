# HERMES_AUDIT — telegram-rag-bot (v2 + Phase-1 fixes)

Full-repo audit performed by Hermes before Phase 2 buildout. Status is truthful at
time of writing; items marked ✅ were fixed during this mission (see HERMES_CHANGELOG.md).

## P0 — Critical (fixed first)

| # | Finding | Where | Status |
|---|---------|-------|--------|
| 1 | `client.get_profile_photos` does not exist in Pyrogram 2.0.106 → AttributeError silently emptied every `pfp_hash` (500 live profiles affected) | profiles.py `enrich_user` | ✅ fixed (get_chat_photos async generator + tests) |
| 2 | `PEER_ID_INVALID` from `get_users` on stale ids → no fallback resolver; peers blocked forever | profiles.py | ✅ fixed (`resolve_user` ladder + metrics) |
| 3 | No metrics anywhere; operational visibility limited to prints | repo-wide | ✅ fixed (`metrics.py`, `/prometheus`) |

## P1 — High

| # | Finding | Where | Status |
|---|---------|-------|--------|
| 4 | `profiles._scroll_page` calls Qdrant without the retry/timeout wrapper used elsewhere | profiles.py | ✅ fixed (routed via `db._qdrant_read`) |
| 5 | No structured logging; prints only, no log files, no levels | repo-wide | ✅ fixed (`logging_config.py`: JSON rotating file + console, level via env) |
| 6 | No storage failover: a Qdrant outage/limit stops all ingestion with exceptions | database.py callers | ✅ fixed (`storage.py`: SQLite mirror, degrade/recover, resync; tested) |
| 7 | No group quality gate at join time; spam/scam enters the OSINT store | scraper filters | ✅ fixed (`quality.py` GOOD/SPAM/SCAM/JUNK gate before join/index) |
| 8 | No autonomous discovery: groups only via manual `/join` | — | ✅ fixed (`discovery.py`: seeds/Cyprus-IT/@kiprinfobot/web → queue → muted auto-join of GOOD only) |
| 9 | No natural-language dossier query ("everything on X") | — | ✅ fixed (`query.py` CLI + `POST /query` + bot `/dossier`; `export.py` json/jsonl/csv/html) |
| 10 | Unhandled-exception audit: existing loops all wrapped; new modules must follow the rule | various | ✅ done (ruff+mypy clean; all new loops guarded; watchlist/discovery hooks alert-only) |
| 11 | Graceful shutdown lacks queue flush / control-flag kill switch | main.py | ✅ fixed (SIGINT/SIGTERM → cancel → storage flush → notify; `control_cli.py` flag kill switch + watchdog) |

## P2 — Medium / good-to-have

| # | Finding | Status |
|---|---------|--------|
| 12 | Dockerfile / docker-compose / healthchecks absent | ✅ fixed (healthcheck via `/health`, volumes, localhost-only API) |
| 13 | CI (lint/type/test) absent | ✅ fixed (`.github/workflows/ci.yml`: ruff + mypy + `tests/run_all.py`) |
| 14 | No watchlist alerting | ✅ fixed (`watchlist.py` + scrape-batch scan + scheduler DM flush) |
| 15 | No export formats | ✅ fixed (`export.py`: JSON/JSONL/CSV/HTML dossiers) |
| 16 | No scheduler abstraction (ad-hoc loops in main) | ✅ fixed (`scheduler.py`; main loops still own their lifecycles by design) |
| 17 | Message dedup beyond point-id (no content-hash / near-dup guard) | ⚠ partial: point-id + cluster-side `filter_existing_message_ids`; content-hash/near-dup still open |
| 18 | Media dedup by perceptual hash | ⚠ partial: sha256 profile-photo fingerprint stored; true perceptual-hash dedup still open |
| 19 | Hardcoded secrets | ✔ none found (all env; `.env` git-ignored; sessions ignored) |
| 20 | Sync/async mismatches | ✔ none found: fastembed/OCR/file IO all `to_thread`'d; Qdrant calls threaded + time-boxed |
| 21 | Tests are script-style (not pytest-collectable) | ⚠ documented: `tests/run_all.py` is the supported runner and is what CI executes; pytest conversion remains a known gap |
| 22 | `bot.py` prints raw status dicts (nested) | ⚠ accepted: cosmetic; admin-only |

## Known gaps carried forward

- Live-Telegram verification requires the one interactive userbot re-login
  (`AUTH_KEY_UNREGISTERED`): py_compile/import/tests pass while a dead auth key is
  only visible on a real request — the orchestrator now exits with code 2 and the
  exact re-login command when this happens.
- Degraded SQLite mode serves payload/FTS reads only (no vector similarity) — by
  design; vector search resumes after Qdrant recovery + resync.
- pytest-style test conversion (#21).
- `@kiprinfobot` adapter relies on its public menu text; if the bot changes its UI,
  the adapter degrades to "no results" rather than erroring (guarded + logged).
