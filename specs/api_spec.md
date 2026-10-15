# Spec: `api.py` — FastAPI health/heartbeat/control surface

Replace the stub in `api.py`. Python 3.11, full type hints. IMPORT-SAFE: no uvicorn
server start at import; FastAPI app built by a factory. No prints at import.

## Imports

`fastapi` (FastAPI, Depends, Header, HTTPException), `uvicorn` imported ONLY inside
`run_api` (so importing api.py without uvicorn still works for tests with TestClient),
plus `asyncio, time, config, database as db, scraper, profiles, scraper.TargetManager`.

## Auth dependency

```python
async def _require_token(authorization: str = Header(default="")) -> None:
```
- If `config.API_TOKEN` is empty → raise HTTPException(503, "Control API disabled (API_TOKEN not set)").
- Expect `Authorization: Bearer <token>`; missing/mismatched → HTTPException(401, "Unauthorized") using hmac.compare_digest.

## App factory

```python
def create_app() -> FastAPI:
    app = FastAPI(title="telegram-rag-bot", version="2.0.0")
    ...
    return app
```

### Routes

- `GET /health` (public): `{"status": "ok", "uptime_seconds": float, "scraper_running": bool, "workers_active": int, "paused": bool}` — from `scraper.get_control().status()`; never raises (try/except → minimal {"status":"degraded"}).
- `GET /status` (token): `{"scraper": control.status(), "config": config.config_summary()}`.
- `GET /metrics` (token): `{"counters": control.counters, "flood_events_total": ..., "db": await db.collection_stats()}`.
- `POST /control/scraper` (token): body JSON `{"action": "pause"|"resume"}` (pydantic model `ScraperAction` with `action: str`). pause → `control.pause("api")`; resume → `control.resume()`; invalid → 400. Returns `{"ok": true, "paused": bool}`.
- `POST /control/join` (token): body `{"peer": str}` (pydantic `JoinRequest`). `tm = TargetManager(); tm.load(); added = await tm.add(peer)` → `{"ok": true, "added": added}`; empty peer → 400.
- `POST /control/sweep` (token): starts `asyncio.create_task(profiles.build_profiles(client=None, gate=control.gate or scraper.FloodGate(), rate=scraper.RateGate(config.TG_OPS_PER_SECOND, config.TG_OPS_BURST)))` — BUT build_profiles with client=None cannot enrich; instead schedule `profiles.profile_sweep`? No: build_profiles(client,...) — the API has no userbot client here, so expose the hook as a fire-and-forget `_sweep_fn` module-level variable that main.py sets to a callable `async def () -> dict`. If unset → 503 "sweep endpoint not wired (main.py sets api._sweep_fn)". If set: `task = asyncio.create_task(_sweep_fn())` and return {"ok": true, "started": true}.
- Use `app.on_event`? NO — plain routes only (no lifespan hooks needed).

## Server runner for main.py

```python
async def run_api(host: str, port: int) -> None:
    """Run uvicorn in-process (called as an asyncio task by main.py)."""
```
- `import uvicorn` inside; `server = uvicorn.Server(uvicorn.Config(create_app(), host=host, port=port, log_level="info"))`; `await server.serve()`.
- Cancellation-safe: on `asyncio.CancelledError` → `server.should_exit = True`; `await asyncio.sleep(0.5)`; re-raise.

## Module-level

```python
_sweep_fn: Optional[Callable[[], Awaitable[dict]]] = None    # wired by main.py
```

## Notes

- All route handlers wrapped so unexpected exceptions → HTTPException(500, str(e)[:200]).
- Bind address comes from config only (default 127.0.0.1) — never 0.0.0.0 unless env says so.
- TestClient compatibility: importable without uvicorn (uvicorn import inside run_api).
