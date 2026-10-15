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
| 5 | No structured logging; prints only, no log files, no levels | repo-wide | 🚧 queued (this mission) |
| 6 | No storage failover: a Qdrant outage/limit stops all ingestion with exceptions | database.py callers | 🚧 queued (this mission) |
| 7 | No group quality gate at join time; spam/scam enters the OSINT store | scraper filters | 🚧 queued (this mission) |
| 8 | No autonomous discovery: groups only via manual `/join` | — | 🚧 queued (this mission) |
| 9 | No natural-language dossier query ("everything on X") | — | 🚧 queued (this mission) |
| 10 | Unhandled-exception audit: existing loops all wrapped; new modules must follow the rule | various | 🚧 ongoing (this mission) |
| 11 | Graceful shutdown lacks queue flush / control-flag kill switch | main.py | 🚧 queued (this mission) |

## P2 — Medium / good-to-have

| # | Finding | Status |
|---|---------|--------|
| 12 | Dockerfile / docker-compose / healthchecks absent | 🚧 queued (this mission) |
| 13 | CI (lint/type/test) absent | 🚧 queued (this mission) |
| 14 | No watchlist alerting | 🚧 queued (this mission) |
| 15 | No export formats | 🚧 queued (this mission) |
| 16 | No scheduler abstraction (ad-hoc loops in main) | 🚧 queued (this mission) |
| 17 | Message dedup beyond point-id (no content-hash / near-dup guard) | 🚧 queued (this mission) |
| 18 | Media dedup by perceptual hash | 🚧 queued (this mission) |
| 19 | Hardcoded secrets | ✔ none found (all env; `.env` git-ignored; sessions ignored) |
| 20 | Sync/async mismatches | ✔ none found: fastembed/OCR/file IO all `to_thread`'d; Qdrant calls threaded + time-boxed |
| 21 | Tests are script-style (not pytest-collectable) | ⚠ documented; `tests/run_all.py` is the supported runner; pytest conversion remains a known gap |
| 22 | `bot.py` prints raw status dicts (nested) | ⚠ accepted: cosmetic; admin-only |

## Known gaps carried forward

- Live-Telegram verification of Phase-1 fixes (deferred to next restart by design —
  see changelog rationale).
- pytest-style test conversion (#21).
- `@kiprinfobot` adapter relies on its public menu text; if the bot changes its UI,
  the adapter degrades to "no results" rather than erroring (guarded + logged).
