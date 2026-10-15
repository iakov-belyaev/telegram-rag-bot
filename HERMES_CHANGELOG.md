# HERMES_CHANGELOG

Running log of every change Hermes (autonomous agent) makes to this repository.
Format: phase → change → rationale → verification. Newest entries on top.

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
