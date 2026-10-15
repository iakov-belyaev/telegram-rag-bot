# Spec: `main.py` — unified async orchestrator

New file. Python 3.11. Import-safe (no clients created at import). Running it starts
everything: userbot, global bot, parallel scraper, profile sweeps, FastAPI, StorageShield.

## Imports

`asyncio, signal, time, sys`, `from pyrogram import Client, errors`, plus
`config, database as db, profiles, scraper, bot as bot_mod, api`.

Own logger: `def log(message: str) -> None: print(f"[main {time.strftime('%H:%M:%S')}] {message}", flush=True)`.

## Client factory

```python
def _build_clients() -> tuple[Client, Client]:
    userbot = Client(config.SESSION_USERBOT, api_id=config.TELEGRAM_API_ID, api_hash=config.TELEGRAM_API_HASH)
    bot_client = Client(config.SESSION_BOT, api_id=config.TELEGRAM_API_ID, api_hash=config.TELEGRAM_API_HASH, bot_token=config.BOT_TOKEN)
    return userbot, bot_client
```

## Admin notification + StorageShield

```python
async def _notify_admin(bot_client, admin_id: int, text: str) -> None:
    """Best-effort DM to the admin; never raises. PeerIdInvalid → hint to /start the bot."""
```
- Catch `errors.PeerIdInvalid` → log "admin must /start the bot first"; catch generic Exception → log.

```python
async def storage_shield(bot_client, admin_id: int, control) -> None:
    """Capacity guard loop (config.STORAGE_CHECK_INTERVAL_SECONDS cadence).

    usage = stats["messages_usage"]["usage_ratio"]
    - usage >= STORAGE_PAUSE_RATIO and not control.paused → control.pause("storage:high-water") + critical DM
    - usage >= STORAGE_WARN_RATIO and not warned_this_cycle → warn DM once (set warned=True)
    - usage < STORAGE_WARN_RATIO → reset warned; if control.paused and control.pause_reason.startswith("storage")
      → control.resume() + "resumed" DM
    Loop exits on control.stop_requested. All exceptions logged, never fatal.
    """
```

## Main

```python
async def main() -> int:
    """Validate config, start everything, wait for a stop signal. Returns exit code."""
```
Flow:
1. `problems = config.validate_config()`; if problems → print each (prefix " - ") and return 1.
2. `config.ensure_state_dir()`.
3. `userbot, bot_client = _build_clients()`.
4. `await userbot.start()`; `me = await userbot.get_me()`; `admin_id = me.id` (the creator's account).
5. `bot_mod.register_handlers(bot_client, admin_id)`; `await bot_client.start()`.
6. Wire API sweep hook: `api._sweep_fn = _make_sweep_fn(userbot)` where

```python
def _make_sweep_fn(userbot):
    async def _sweep() -> dict:
        control = scraper.get_control()
        gate = getattr(control, "gate", None) or scraper.FloodGate()
        rate = scraper.RateGate(config.TG_OPS_PER_SECOND, config.TG_OPS_BURST)
        return await profiles.build_profiles(userbot, gate, rate)
    return _sweep
```

7. Stop event + signal handlers:
```python
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
```
8. Start supervised tasks (each wrapped so a crash of one task is logged and triggers shutdown):
```python
    control = scraper.get_control()
    tasks = [
        asyncio.create_task(scraper.run_scraper(userbot), name="scraper"),
        asyncio.create_task(profiles.profile_sweep(userbot, control), name="profiles"),
        asyncio.create_task(api.run_api(config.API_HOST, config.API_PORT), name="api"),
        asyncio.create_task(storage_shield(bot_client, admin_id, control), name="storage-shield"),
    ]
    await _notify_admin(bot_client, admin_id,
        f"🚀 telegram-rag-bot v2 online\nBot: {config.BOT_USERNAME}\n"
        f"Admin id: {admin_id}\nWorkers: {config.SCRAPER_WORKERS}\n"
        f"API: http://{config.API_HOST}:{config.API_PORT}/health")
```
9. `await stop.wait()` → shutdown:
```python
    log("stop signal received — shutting down")
    control.request_stop()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await _notify_admin(bot_client, admin_id, "🛑 telegram-rag-bot shutting down")
    await userbot.stop()
    await bot_client.stop()
    return 0
```
10. Startup failures (userbot/bot start errors) → catch, log, return 1.

## Sync entry

```python
def _run() -> int:
    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        log("interrupted")
        return 130

if __name__ == "__main__":
    raise SystemExit(_run())
```

## Notes

- Do NOT install signal handlers outside the running loop.
- No `loop.run_until_complete` legacy style.
- Keep every awaitable in try/except at the top-level main so tracebacks are logged cleanly.

---

# Spec: `setup_db.py` — Qdrant provisioning (rewrite)

Replace the existing v1 setup_db.py entirely (fresh implementation of the same file).
Create ALL FOUR collections from config with `config.VECTOR_SIZE` / Cosine, then ensure
payload indexes. Idempotent: safe to re-run; never deletes existing data.

```python
import config
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PayloadSchemaType, VectorParams

def build_client() -> QdrantClient:   # url+api_key from config; fall back to local path 'qdrant_data' when QDRANT_URL empty
def ensure_collection(client, name) -> None:   # exists → verify size/distance match (warn + recreate ONLY if mismatched, printing loudly); else create
def ensure_index(client, collection, field_name, schema) -> None:  # try create_payload_index; HTTP 400 "already exists" → print ok; raise other errors
def main() -> int:
    client = build_client()
    for name in (MESSAGES, CHATS, PROFILES, RELATIONS): ensure_collection(...)
    # messages indexes: chat_id INTEGER, sender_id INTEGER, sender_username KEYWORD, source_type KEYWORD
    # chats indexes: chat_id INTEGER
    # profiles indexes: user_id INTEGER, username KEYWORD
    # relations indexes: a_id INTEGER, b_id INTEGER, edge_kind KEYWORD
    print summary lines (points_count per collection via get_collection)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
```
- Use `PayloadSchemaType.INTEGER` for numeric ids (Qdrant supports integer match), KEYWORD otherwise.
- Wrap per-index creation in try/except; a duplicate-index error must NOT abort the run.
- Print clear lines: "created", "already exists (size ok)", "indexed <field>", "index exists".
