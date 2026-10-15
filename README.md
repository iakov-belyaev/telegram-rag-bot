# telegram-rag-bot v2 — Autonomous Cyprus OSINT & RAG Intelligence Engine

A dual-engine Telegram intelligence system backed by the **tg_bot_rag** Qdrant
cluster database. A Pyrogram **userbot** continuously ingests chats in parallel;
a global **bot** answers anywhere via natural language; deep OSINT dossiers are
gated behind an admin **challenge/response** handshake. Ingestion survives Qdrant
outages via a transparent local failover store, and the system expands its own
coverage through an autonomous, quality-gated discovery agent.

---

## Architecture

```
              ┌─────────────────────────┐        ┌──────────────────────────┐
              │  Userbot (Pyrogram)     │        │  Global Bot (Pyrogram)   │
              │  session: my_userbot    │        │  session: inline_bot     │
              └────────────┬────────────┘        └────────────┬─────────────┘
     parallel workers      │                                  │  @mentions / inline / private
     (SCRAPER_WORKERS)     ▼                                  ▼
   ┌───────────────────────────────┐              ┌──────────────────────────────┐
   │ scraper.py                    │              │ bot.py                       │
   │ • FloodGate (global pause)    │              │ • casual answers (public)    │
   │ • RateGate (token bucket)     │              │ • /auth challenge → admin    │
   │ • cursors + filter cache      │              │ • /panel /status /dbstats    │
   │ • multi-modal OCR/PDF/DOCX    │              │ • /whois /introduce /dossier │
   │ • watchlist scan per batch    │              │ • /watch + "everything on X" │
   └──────────────┬────────────────┘              └──────────────┬───────────────┘
                  ▼                                              ▼
   ┌───────────────────────────────────────────────────────────────────────────┐
   │ storage.py — failover-aware storage layer                                 │
   │   Qdrant Cloud (tg_bot_rag): telegram_messages · telegram_chats ·         │
   │     telegram_profiles · telegram_relations                                │
   │   ↕ degrade on errors / recover + resync                                  │
   │   SQLite mirror (state/failover_store.db): same schema + dirty-tracking   │
   └──────────────────────────────▲────────────────────────────────────────────┘
                                  │ profiles.py sweep (bios, photo hashes,
                                  │  common groups, co-member/reply/mention edges)
   ┌──────────────────────────────┴───────────┐   ┌────────────────────────────┐
   │ main.py — orchestrator                   │   │ api.py — FastAPI           │
   │ scraper · profiles · storage-shield ·    │   │ /health /status /metrics   │
   │ discovery · scheduler · flags-watchdog   │   │ /query /prometheus         │
   └──────────────────────────────────────────┘   │ /control/* (Bearer token)  │
   ┌──────────────────────────────────────────┐   └────────────────────────────┘
   │ discovery.py — autonomous expansion      │
   │ seeds.yaml · Cyprus-IT scan · @kiprinfobot│
   │ web adapter (off by default) → queue      │
   │ quality.py gate: GOOD/SPAM/SCAM/JUNK      │
   │ auto-join (muted) only GOOD groups        │
   └──────────────────────────────────────────┘
```

Key design points:

- **Non-blocking parallel ingestion.** `asyncio` worker pool over a chat queue;
  every Telegram call passes a global `RateGate` (token bucket) and a shared
  `FloodGate`: when *any* worker hits `FloodWait`, all workers park until the
  flood window clears (with margin + jitter), then resume.
- **Resumable scraping.** Per-chat pagination cursors persist to `state/`;
  a cluster-side dedup guard (`filter_existing_message_ids`) prevents re-embedding.
- **Transparent storage failover.** `storage.py` mirrors every write into SQLite
  while serving Qdrant. After `FAILOVER_DEGRADE_ERRORS` consecutive Qdrant errors
  it degrades to the SQLite mirror (reads and writes keep working, dirty rows are
  tracked); a health monitor probes Qdrant and, on recovery, resyncs the dirty
  rows back with fresh embeddings — no data loss, no manual steps.
- **Truthful profiles.** The profile sweep aggregates real indexed messages and
  enriches them with live Telegram data (bio, sha256-projected photo hash +
  metadata). Relations are materialized as `co_member`, `replies`, `mentions`
  edges with weights.
- **Gated OSINT.** Public surfaces only return casual summaries. Deep dossiers
  require the private admin handshake: `/auth` → nonce challenge → on the server
  `python rbac.py respond <nonce>` → reply with the code → sliding TTL session.
- **Autonomous discovery, quality-gated.** Discovery adapters (seed file,
  Cyprus-IT chat link extraction, `@kiprinfobot` menu walker, optional web
  search) feed one persistent queue. Before any join, `quality.py` classifies
  the group (spam/scam/junk heuristics + optional pluggable LLM hook); only
  GOOD groups are joined — and every join is **muted immediately**.
- **Natural-language dossier query.** `query.py` parses entities (phone, @username,
  user_id, email, wallet, t.me link, free text), fans out across messages,
  profiles, relations and chats, and returns a structured dossier — via CLI
  (`python query.py "give me everything on +357..."`), HTTP (`POST /query`),
  the bot (`/dossier`, or just ask `everything on X` in private chat).
- **Watchlist + scheduler + kill switch.** Tracked tokens are scanned against
  every ingested batch; hits queue up and the scheduler DMs them to the admin.
  `python control_cli.py pause|resume|stop|status` writes a flag file that the
  orchestrator's watchdog applies within seconds — a remote kill switch.
- **StorageShield.** Capacity check on `telegram_messages`; warns at 80%,
  pauses all ingestion at 95%, DMs the admin, auto-resumes on recovery.

## Modules

| File | Role |
|---|---|
| `config.py` | Single source of truth for env settings + `validate_config()` |
| `database.py` | Qdrant-native layer: lazy clients, payload builders, batched upserts, retrying reads |
| `storage.py` | Failover-aware layer used by all callers: Qdrant + SQLite mirror, degrade/recover/resync |
| `scraper.py` | FloodGate, RateGate, worker pool, cursors, filter cache, target joins, live listener, watchlist scan |
| `profiles.py` | Profile aggregation + enrichment + relation graph + `whois()`/`introduce()` |
| `quality.py` | Group quality classifier: GOOD / SPAM / SCAM / JUNK |
| `discovery.py` | Discovery queue + adapters (seeds, Cyprus-IT, @kiprinfobot, web) + join agent |
| `query.py` | Entity parsing + dossier fan-out + rendering (CLI/HTTP/bot) |
| `export.py` | Dossier exports: JSON, JSONL, CSV, HTML |
| `watchlist.py` | Token watchlist + match + hits queue |
| `scheduler.py` | Minimal interval scheduler for background jobs |
| `control_cli.py` | `pause`/`resume`/`stop`/`status` CLI + flag watcher |
| `rbac.py` | HMAC challenge/response admin auth (+ CLI helper) |
| `bot.py` | Global bot: NLP routing, casual/deep answers, admin panel, watchlist, dossiers, inline |
| `api.py` | FastAPI: `/health`, `/status`, `/metrics`, `/query`, `/prometheus`, `/control/*` |
| `logging_config.py` | JSON structured logging (rotating file + console) |
| `metrics.py` | Dependency-free counters/gauges + Prometheus exposition |
| `main.py` | Orchestrator: clients, tasks, scheduler, watchdog, signals, graceful shutdown |
| `setup_db.py` | Creates collections + payload indexes (idempotent) |
| `specs/` | Build-time specifications used to generate the modules |

## Quick start

```bash
# 1. Environment
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env   # fill in Telegram, Qdrant, DeepSeek, ADMIN_SECRET, API_TOKEN
cp seeds.yaml.example seeds.yaml   # optional: bootstrap groups

# 3. Provision the cluster (safe to re-run)
python setup_db.py

# 4. First run authorizes the userbot session (interactive: phone → code)
python main.py
```

Run in background: `nohup ./start.sh > bot.log 2>&1 &`
Docker: `docker compose up -d` (copy session files into the container first —
interactive login cannot run headless; see docker-compose.yml notes).

## Interacting

**Anyone, anywhere:**
- Inline or group mention: `@<bot_username> who is @someone?` → casual summary only.
- Deep asks always answer with the casual view + a "🔒 authenticate" hint.

**Admin (private chat):** `/auth` → challenge → verify. Then:
`/panel` · `/status` · `/dbstats` · `/scrape start|pause|resume` ·
`/join <@group|t.me/link|id>` · `/targets` ·
`/whois <@user|id>` · `/introduce <a> <b>` · `/sweep [full]` · `/logout` ·
`/watch add|remove|list <token>` · `/dossier <entity>` — or ask naturally:
`everything on @someone`.

## Query / Export / Control CLIs

```bash
python query.py "give me everything on +357 99 123456"      # dossier (text)
python query.py "@someone" --json --limit 100               # machine output
python export.py "@someone" --format all --out state/exports # json/jsonl/csv/html
python control_cli.py pause | resume | stop | status        # remote kill switch
python rbac.py respond <nonce>                              # auth challenge response
python tests/run_all.py                                     # full test suite
```

## Control API

`GET /health` — public liveness. Token-gated (Bearer):
`GET /status` (includes storage failover state), `GET /metrics`, `GET /prometheus`,
`POST /query {"query": "...", "limit": 50}`,
`POST /control/scraper {"action":"pause|resume"}`,
`POST /control/join {"peer":"@group"}`, `POST /control/sweep`.
Binds to `127.0.0.1` by default.

## Tests & CI

All suites are plain-python scripts (no pytest needed) and are run in one shot:

```bash
python tests/run_all.py
```

18 suites cover: payload builders + live embed, scraper gates/cursors/workers,
profiles aggregation/relations, phase-1 fixes, RBAC, bot NLP/panel, API clients,
orchestrator shield, quality classifier, discovery adapters, storage failover
(degrade/recover/resync), query dossiers, watchlist, scheduler, control CLI,
exports, main wiring, and a read-only live aggregation over the real cluster.

CI (`.github/workflows/ci.yml`): ruff + mypy + `tests/run_all.py` on push.

## Production notes

- Keep `.env` private; it is git-ignored. Session files (`*.session`) never commit.
- `state/` holds cursors, queue, watchlist, control flags, failover SQLite,
  exports — back it up, it is the resume state.
- A degraded (SQLite) mode keeps serving reads; check `GET /status` →
  `failover.mode`. Resync happens automatically after Qdrant recovers.
- Auto-joined groups are muted before any other action; only quality-GOOD
  groups are ever joined.
