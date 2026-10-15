# telegram-rag-bot v2 — Parallel OSINT & RAG Intelligence Engine

A dual-engine Telegram intelligence system backed by the **tg_bot_rag** Qdrant
cluster database. A Pyrogram **userbot** continuously ingests chats in parallel;
a global **bot** answers anywhere via natural language; deep OSINT profiles are
gated behind an admin **challenge/response** handshake.

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
   │ • cursors + filter cache      │              │ • /panel, /status, /dbstats  │
   │ • multi-modal OCR/PDF/DOCX    │              │ • /whois, /introduce (deep)  │
   └──────────────┬────────────────┘              └──────────────┬───────────────┘
                  ▼                                              ▼
   ┌───────────────────────────────────────────────────────────────────────────┐
   │ database.py  →  Qdrant Cloud (tg_bot_rag)                                 │
   │   telegram_messages · telegram_chats · telegram_profiles · telegram_relations │
   └──────────────────────────────▲────────────────────────────────────────────┘
                                  │ profiles.py sweep (bios, photo hashes,
                                  │  common groups, co-member/reply/mention edges)
   ┌──────────────────────────────┴───────────┐   ┌────────────────────────────┐
   │ main.py — orchestrator                   │   │ api.py — FastAPI           │
   │ userbot+bot+scraper+profiles+shield      │   │ /health /status /metrics   │
   └──────────────────────────────────────────┘   │ /control/* (Bearer token)  │
                                                  └────────────────────────────┘
```

Key design points:

- **Non-blocking parallel ingestion.** `asyncio` worker pool over a chat queue;
  every Telegram call passes a global `RateGate` (token bucket) and a shared
  `FloodGate`: when *any* worker hits `FloodWait`, all workers park until the
  flood window clears (with margin + jitter), then resume — no thundering herd.
- **Resumable scraping.** Per-chat pagination cursors persist to `state/`, so
  restarts resume instead of re-walking history. A cluster-side dedup guard
  (`filter_existing_message_ids`) prevents re-embedding already-indexed messages.
- **Truthful profiles.** The profile sweep aggregates real indexed messages
  (counts, active groups, reply/mention interactions) and enriches them with live
  Telegram data (bio, 256-bit-projected profile-photo hash + metadata). Relations
  are materialized as `co_member`, `replies`, `mentions` edges with weights.
- **Gated OSINT.** Public surfaces (groups, inline) only ever return casual
  summaries. Deep dossiers require the private admin handshake:
  1. Admin sends `/auth` in the bot's private chat → nonce challenge.
  2. On the server: `python rbac.py respond <nonce>` → response code.
  3. Admin sends the code → authenticated session (sliding TTL, lockout on
     failed attempts, single-use nonce). Only then do `/whois`, `/introduce`,
     `/panel`, `/scrape` … unlock.
- **StorageShield.** Hourly capacity check on `telegram_messages`; warns at 80%,
  pauses all ingestion at 95%, DMs the admin.

## Modules

| File | Role |
|---|---|
| `config.py` | Single source of truth for all env-driven settings + `validate_config()` |
| `database.py` | Lazy Qdrant/FastEmbed clients, payload builders, batched upserts, retrying reads |
| `scraper.py` | FloodGate, RateGate, worker pool, cursors, chat filter cache, target joins, live listener |
| `profiles.py` | Profile aggregation + Telegram enrichment + relation graph + `whois()`/`introduce()` |
| `rbac.py` | HMAC challenge/response admin auth (+ CLI helper) |
| `bot.py` | Global interaction bot: NLP routing, casual/deep answers, admin panel, inline mode |
| `api.py` | FastAPI: `/health` (public), `/status`, `/metrics`, `/control/*` (Bearer token) |
| `main.py` | Orchestrator: clients, tasks, signals, graceful shutdown |
| `setup_db.py` | Creates the four collections + payload indexes (idempotent) |
| `specs/` | Build-time specifications used to generate the modules |

## Quick start

```bash
# 1. Environment
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env   # fill in Telegram, Qdrant, DeepSeek, ADMIN_SECRET, API_TOKEN

# 3. Provision the cluster (safe to re-run)
python setup_db.py

# 4. First run authorizes the userbot session (code from Telegram)
python main.py
```

Run in background: `nohup ./start.sh > bot.log 2>&1 &`

## Interacting

**Anyone, anywhere:**
- Inline: `@<bot_username> what did we discuss about X?`
- In a group where the bot is present: `@<bot_username> who is @someone?` → casual summary only.
- Deep asks always answer with the casual view + a "🔒 authenticate" hint.

**Admin (private chat):**
`/auth` → challenge; verify; then:
`/panel` (buttons: status, DB metrics, pause/resume, targets, refresh) ·
`/status` · `/dbstats` · `/scrape start|pause|resume` ·
`/join <@group|t.me/link|id>` (queues a join target for the userbot) ·
`/targets` · `/whois <@user|id>` · `/introduce <a> <b>` · `/sweep` (on-demand profile rebuild) · `/logout`

## Control API

`GET /health` — public liveness.
`GET /status`, `GET /metrics`, `POST /control/scraper` `{"action":"pause|resume"}`,
`POST /control/join` `{"peer":"@group"}`, `POST /control/sweep` — require
`Authorization: Bearer $API_TOKEN`. Binds to `127.0.0.1` by default.

## Tests

Plain-python test scripts (no pytest needed), run from the repo root:

```bash
python tests/test_database.py     # payload builders + live embed + dedup guard
python tests/test_scraper.py      # gates, cursors, filter cache, pagination (mocked)
python tests/test_profiles.py     # aggregation, relations, sweep (mocked)
python tests/test_rbac.py         # challenge/response, lockout, CLI helper
python tests/test_bot.py          # NLP gating, auth flow, panel callbacks (mocked)
python tests/test_api.py          # TestClient: auth, control endpoints
python tests/test_main.py         # storage shield lifecycle (mocked)
python tests/test_live_aggregate.py  # read-only sweep over the live cluster
```

## Joining Cyprus networking groups

The account joins groups only from the managed target list — add them as admin
with `/join <peer>` (or the control API). The userbot converges the list on the
next scraper pass (extra flood-sensitive, hence capped concurrency), then the
groups flow into the normal ingest pipeline. This keeps joins deliberate, safe,
and auditable rather than blind mass-joining.
