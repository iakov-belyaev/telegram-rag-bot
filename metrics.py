"""Minimal process-wide metrics registry with Prometheus text exposition.

No external dependencies: counters and gauges are plain floats guarded by a
thread lock (asyncio tasks and worker threads both bump them). Consumers:
api.py /prometheus endpoint and operational dashboards.
"""

from __future__ import annotations

import threading
import time
from typing import Dict

_lock = threading.Lock()
_counters: Dict[str, float] = {}
_gauges: Dict[str, float] = {}
_started_at = time.time()


def inc(name: str, amount: float = 1.0) -> None:
    """Increment a counter."""
    with _lock:
        _counters[name] = _counters.get(name, 0.0) + amount


def set_gauge(name: str, value: float) -> None:
    """Set a gauge to an absolute value."""
    with _lock:
        _gauges[name] = float(value)


def get_counter(name: str) -> float:
    """Read a counter (0.0 when never touched)."""
    with _lock:
        return _counters.get(name, 0.0)


def snapshot() -> dict:
    """JSON-safe snapshot for API endpoints."""
    with _lock:
        return {
            "uptime_seconds": time.time() - _started_at,
            "counters": dict(_counters),
            "gauges": dict(_gauges),
        }


def _sanitize(name: str) -> str:
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in name)


def render_prometheus() -> str:
    """Prometheus text-format exposition of all counters + gauges."""
    lines: list[str] = []
    with _lock:
        counters = dict(_counters)
        gauges = dict(_gauges)
    for name in sorted(counters):
        metric = _sanitize(f"tgbot_{name}")
        lines.append(f"# TYPE {metric} counter")
        lines.append(f"{metric} {counters[name]}")
    for name in sorted(gauges):
        metric = _sanitize(f"tgbot_{name}")
        lines.append(f"# TYPE {metric} gauge")
        lines.append(f"{metric} {gauges[name]}")
    lines.append("# TYPE tgbot_uptime_seconds gauge")
    lines.append(f"tgbot_uptime_seconds {time.time() - _started_at:.1f}")
    return "\n".join(lines) + "\n"
