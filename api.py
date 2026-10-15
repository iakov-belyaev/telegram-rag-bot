"""FastAPI heartbeat + control surface for telegram-rag-bot.

Public /health for liveness; token-gated /status, /metrics and /control/*
endpoints for the admin. Started in-process by main.py via run_api().
"""

from __future__ import annotations

import asyncio
import hmac
import time
from typing import Any, Callable, Coroutine, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

import config
import metrics as metrics_lib
import scraper
import storage as db

_START_TIME = time.monotonic()

# Wired by main.py to an async callable that performs a profile sweep.
_sweep_fn: Optional[Callable[[], Coroutine[Any, Any, dict]]] = None


class ScraperAction(BaseModel):
    action: str


class JoinRequest(BaseModel):
    peer: str


class QueryRequest(BaseModel):
    query: str
    limit: Optional[int] = None


async def _require_token(authorization: str = Header(default="")) -> None:
    """Validate the Bearer token against config.API_TOKEN."""
    if not config.API_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Control API disabled (API_TOKEN not set)",
        )
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = authorization[len(prefix):]
    if not hmac.compare_digest(token, config.API_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")


def create_app() -> FastAPI:
    app = FastAPI(title="telegram-rag-bot", version="2.0.0")

    @app.get("/health")
    async def health() -> dict:
        try:
            status = scraper.get_control().status()
            return {
                "status": "ok",
                "uptime_seconds": time.monotonic() - _START_TIME,
                "scraper_running": not bool(status.get("stop_requested", False)),
                "workers_active": int(status.get("workers_active", 0)),
                "paused": bool(status.get("paused", False)),
            }
        except Exception:
            return {"status": "degraded"}

    @app.get("/status", dependencies=[Depends(_require_token)])
    async def status() -> dict:
        try:
            control = scraper.get_control()
            return {
                "scraper": control.status(),
                "config": config.config_summary(),
                "failover": db.storage_status() if hasattr(db, "storage_status") else {},
            }
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(e)[:200]) from e

    @app.get("/metrics", dependencies=[Depends(_require_token)])
    async def metrics() -> dict:
        try:
            control = scraper.get_control()
            gate = getattr(control, "gate", None)
            flood_events_total = (
                gate.flood_events if gate is not None else 0
            )
            return {
                "counters": control.counters,
                "flood_events_total": flood_events_total,
                "db": await db.collection_stats(),
            }
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(e)[:200]) from e

    @app.post("/query", dependencies=[Depends(_require_token)])
    async def run_query(payload: QueryRequest) -> dict:
        try:
            text = (payload.query or "").strip()
            if not text:
                raise HTTPException(status_code=400, detail="query must not be empty")
            import query as query_lib

            return await query_lib.dossier(text, limit=payload.limit)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(e)[:200]) from e

    @app.get("/prometheus", dependencies=[Depends(_require_token)])
    async def prometheus() -> PlainTextResponse:
        try:
            return PlainTextResponse(metrics_lib.render_prometheus())
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(e)[:200]) from e

    @app.post("/control/scraper", dependencies=[Depends(_require_token)])
    async def control_scraper(payload: ScraperAction) -> dict:
        try:
            control = scraper.get_control()
            action = (payload.action or "").lower()
            if action == "pause":
                control.pause("api")
            elif action == "resume":
                control.resume()
            else:
                raise HTTPException(
                    status_code=400, detail="action must be 'pause' or 'resume'"
                )
            return {"ok": True, "paused": bool(control.status().get("paused", False))}
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(e)[:200]) from e

    @app.post("/control/join", dependencies=[Depends(_require_token)])
    async def control_join(payload: JoinRequest) -> dict:
        try:
            peer = (payload.peer or "").strip()
            if not peer:
                raise HTTPException(status_code=400, detail="peer must not be empty")
            tm = scraper.TargetManager()
            tm.load()
            added = await tm.add(peer)
            return {"ok": True, "added": bool(added)}
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(e)[:200]) from e

    @app.post("/control/sweep", dependencies=[Depends(_require_token)])
    async def control_sweep() -> dict:
        try:
            if _sweep_fn is None:
                raise HTTPException(
                    status_code=503,
                    detail="sweep endpoint not wired (main.py sets api._sweep_fn)",
                )
            asyncio.create_task(_sweep_fn())
            return {"ok": True, "started": True}
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(e)[:200]) from e

    return app


async def run_api(host: str, port: int) -> None:
    """Run uvicorn in-process (called as an asyncio task by main.py)."""
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(create_app(), host=host, port=port, log_level="info")
    )
    try:
        await server.serve()
    except asyncio.CancelledError:
        server.should_exit = True
        await asyncio.sleep(0.5)
        raise
