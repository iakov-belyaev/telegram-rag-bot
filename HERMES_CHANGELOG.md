# HERMES_CHANGELOG

Running log of every change Hermes (autonomous agent) makes to this repository.
Format: phase → change → rationale → verification. Newest entries on top.

---

## Phase 2 — Autonomous OSINT platform

**Verification summary:** full suite green — `python tests/run_all.py` → 18/18 suites
pass (incl. storage failover, query, wiring, live read-only aggregation). `ruff` and
`mypy` run clean over all 18 source files. Qdrant `tg_bot_rag` untouched by tests
(read-only paths only).

### 2.1 Quality gate + autonomous discovery + structured logging (commit f2780ff)

- `quality.py` (GOOD/SPAM/SCAM/JUNK heuristics; pluggable LLM hook off by default),
  `discovery.py` (persistent queue; seeds.yaml, Cyprus-IT link extraction,
  `@kiprinfobot` menu adapter, optional web adapters), `logging_config.py`
  (JSON + rotating file). Joined groups are always muted first; only GOOD groups join.
- Bug found during testing: `discovery.py` referenced a nonexistent
  `config.QDRANT_COLLECTION_MESSAGES` (silently swallowed) → fixed to
  `config.MESSAGES_COLLECTION`.

### 2.2 Storage failover layer (`storage.py`)

- All callers (`scraper`, `profiles`, `bot`, `api`, `discovery`, `main`) switched from
  `database` to `storage`: Qdrant primary, SQLite mirror (`state/failover_store.db`),
  degrade after `FAILOVER_DEGRADE_ERRORS` consecutive failures, dirty-row tracking,
  automatic probe + resync on recovery (fresh embeddings, batched).
- Decisions (documented): rows are marked dirty on sqlite writes and cleared only
  after successful re-upload; `_use_qdrant()` resets the error counter (otherwise
  the first post-recovery hiccup instantly re-degrades); reads served from SQLite
  while degraded are payload+FTS only (no vector search in degraded mode).
- Bugs found by the failover test suite and fixed: `fetch_dirty` read `rowid`
  without selecting it (`IndexError` on every resync) → `SELECT rowid AS rowid, *`;
  `_use_qdrant` stale-counter re-degrade (above).

### 2.3 Natural-language query interface (`query.py`) + exports

- Entity detection: phone / @username / user_id / email / wallet (ETH+BTC) / t.me
  link / free text; fan-out over messages, profiles, relations, chats, participants;
  structured dossier with timeline + stats; text renderer; `--json` CLI.
- Decision: bare digit-runs (no `+`/separators) classify as `user_id`, not `phone`
  (Telegram ids are 5-15 digits) — phones require a `+` or separator formatting, and
  bio matching is digit-normalized (`+357 99 123456` matches `+35799123456`).
- `export.py`: dossier → JSON / JSONL / CSV / HTML (escaped).
- Surfaces: CLI, `POST /query`, bot `/dossier` + natural phrase "everything on X".

### 2.4 Support modules

- `watchlist.py` (tokens scanned against every ingested batch; hits queued to
  `state/watchlist_hits.jsonl`), `scheduler.py` (interval jobs), `control_cli.py`
  (pause/resume/stop/status flag CLI + watcher), exports. Watchlist scan hooked into
  `scraper.index_messages_batch` (alert-only; never blocks ingestion).
- Decision: purely-numeric watchlist tokens need >= 6 digits and match against the
  digit-normalized text; letter-bearing tokens use case-insensitive substring —
  short numeric tokens no longer fire on every message.

### 2.5 Orchestrator wiring (`main.py`)

- Boot: structured logging → clients → storage start → scraper, profiles, API,
  StorageShield, discovery (if enabled), scheduler (watchlist flush),
  flags-watchdog → notify. Shutdown: stop flag → cancel tasks → storage flush →
  notify. Userbot `AUTH_KEY_UNREGISTERED` gets a targeted message with the exact
  re-login command (exit code 2).
- `config.py`: `WATCHLIST_FLUSH_SECONDS`; discovery/failover/log/watchlist blocks.

### 2.6 Hardening

- `ruff` clean (E/F/W/B/I), `mypy` clean (18 modules, lenient-but-real config).
  Fixed: 7x B904 (exception chaining in api.py), B023 (scraper consumer closure
  now binds its cycle's queue explicitly), embedding-pair `zip()` → `strict=True`
  (misaligned vectors now fail loudly instead of silently truncating), path
  annotations, openai-stub ignores.
- `Dockerfile` + `docker-compose.yml` (healthchecks, volumes, localhost-only API),
  `.github/workflows/ci.yml` (ruff + mypy + tests), `pyproject.toml`,
  `requirements-dev.txt`, `seeds.yaml.example`; deps fixed (`httpx`, `pyyaml`,
  `pydantic` were used but undeclared).

---

## Phase 1 — Critical fixes (profile scraping)

**Verification summary:** `tests/test_phase1_fixes.py` — 20/20 checks pass; related
suites (`test_profiles`, `test_bot`, `test_api`, `test_scraper`) stay green.
Live-Telegram re-verification is deferred to the next production restart because a
`main.py` process (pid 33032, started 22:48) already holds the only session — running
a second client concurrently risks `AUTH_KEY_DUPLICATED` killing the fresh login.
Evidence of the bug's live impact: the 500 profiles indexed at 22:50 all carry
`pfp_hash=''` (checked directly against the tg_bot_rag cluster).

### 1.1 Profile photo fetch — `AttributeError: 'Client' object has no attribute 'get_profile_photos'`

- **Library detected:** Pyrogram 2.0.106 only (Repo-wide grep; no Telethon anywhere).
- **Root cause:** Pyrogram ≥2.0 exposes profile photos as the **async generator**
  `Client.get_chat_photos(chat_id, limit=0)` (verified in the installed package:
  `.venv/.../pyrogram/methods/users/get_chat_photos.py`). `get_profile_photos` was
  removed; and because the method is an async generator it must be iterated
  (`async for`), never awaited — our `telegram_call()` helper only awaits plain
  coroutines.
- **Fix (`profiles.py`):** photo fetch now iterates
  `iterate_with_flood(gate, client.get_chat_photos, user_id, limit=1)`, picks the
  smallest `PhotoSize` by `file_size` (fallback: pixel area), downloads via its
  `file_id` (in-memory), and stores the sha256 digest truncated to
  `PFP_HASH_SIZE//4` hex chars — same schema as before.
- **Decision (documented):** we store a **hash + metadata** (width/height/file_size),
  not a file path. Rationale: the OSINT schema intentionally stores no images, only
  fingerprints; paths would rot and leak file layout. Failure path logs,
  keeps old values, bumps `profile_photo_failures` metric, never raises.

### 1.2 `PEER_ID_INVALID` on `get_users` for uncached ids

- **Fix (`profiles.py`):** new `resolve_user(client, gate, user_id, context_chat_id)`
  ladder — direct `get_users` → `PeerIdInvalid` → scan participants of the context
  chat (capped at `RESOLVE_CONTEXT_SCAN_LIMIT=1000` items so one uncached peer can
  never walk a 10k-member chat) → `get_chat(user_id)` cache warm-up + retry →
  give up with `None`.
- **Decision:** the ladder never raises; unresolved peers are skipped, counted
  (`peer_unresolved`, `peers_resolved_via_context`, `peers_resolved_via_warmup`),
  and the sweep continues. `build_profiles` passes the user's most-active chat as
  the context hint for the members-scan step.

### 1.3 New `metrics.py` (foundation for Phase 2 metrics)

- Dependency-free thread-safe counters/gauges + Prometheus text exposition.
- Wired: `flood_wait_events` (FloodGate trips), `messages_scraped` (ingest),
  `peers_resolved*`/`peer_unresolved`/`profile_photo_failures`,
  `profile_sweep_runs`/`profile_sweep_user_errors`/`profile_sweep_last_duration_seconds`,
  `profile_sweep_failures`.
- `api.py`: new token-gated `GET /prometheus` endpoint (text format).
- **Scoping bug found & fixed during testing:** the route fn `metrics()` defined
  inside `create_app()` shadowed the `metrics` module import for sibling closures
  → `/prometheus` returned 500. Import aliased as `metrics_lib`.

### 1.4 `force_refresh` plumbing

- `profiles.build_profiles(..., force_refresh=False)` and `enrich_user(..., force_refresh=False)`:
  skip the 24 h freshness fast-path.
- Bot: `/sweep full` (`/sweep --full`) triggers a full refresh — needed to backfill
  the 500 profiles whose `pfp_hash` was emptied by bug 1.1 before the fix.
- API sweep hook unchanged (defaults to incremental).

### Housekeeping

- All edits were made against the running-process-safe files only (Python has no
  hot reload: pid 33032 keeps executing the old code until restarted).
