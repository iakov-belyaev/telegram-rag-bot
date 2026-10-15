"""Tiny interval-job scheduler for main.py (no external dependencies).

Interval jobs (not cron): each job runs every N seconds when due, sequentially,
with exceptions caught and counted. Suits the discovery/resync/flush cadence of
this bot without pulling in APScheduler.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional

import metrics
from logging_config import get_logger

_logger = get_logger("scheduler")


def log(message: str) -> None:
    _logger.info("%s", message)


class Job:
    def __init__(self, name: str, interval: float, fn: Callable[[], Awaitable], run_immediately: bool = False) -> None:
        self.name = name
        self.interval = float(interval)
        self.fn = fn
        self.next_at = time.monotonic() + (0.0 if run_immediately else self.interval)
        self.runs = 0
        self.errors = 0
        self.last_run: Optional[float] = None


class Scheduler:
    """Sequential interval scheduler; honours control.stop_requested when given."""

    def __init__(self, control=None) -> None:
        self.control = control
        self.jobs: dict[str, Job] = {}

    def add(self, name: str, interval: float, fn: Callable[[], Awaitable], run_immediately: bool = False) -> None:
        self.jobs[name] = Job(name, interval, fn, run_immediately)

    def status(self) -> list[dict]:
        now = time.monotonic()
        return [{
            "name": j.name,
            "interval": j.interval,
            "runs": j.runs,
            "errors": j.errors,
            "due_in": max(0.0, round(j.next_at - now, 1)),
        } for j in self.jobs.values()]

    async def run(self) -> None:
        """Forever: run due jobs, sleep 1s ticks."""
        while True:
            if self.control is not None and getattr(self.control, "stop_requested", False):
                log("scheduler stopping (stop requested)")
                return
            now = time.monotonic()
            for job in list(self.jobs.values()):
                if now < job.next_at:
                    continue
                job.next_at = time.monotonic() + job.interval
                try:
                    await job.fn()
                    job.runs += 1
                    job.last_run = time.time()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    job.errors += 1
                    metrics.inc("scheduler_job_errors")
                    log(f"⚠️ job {job.name} failed: {e}")
            await asyncio.sleep(1.0)
