"""Remote control via flag file — kill switch, pause, resume.

Usage:
    python control_cli.py pause      # pause ingestion (safe)
    python control_cli.py resume
    python control_cli.py stop       # graceful full shutdown (kill switch)
    python control_cli.py status     # show current flags

main.py polls config.CONTROL_FLAGS_FILE (CONTROL_FLAG_POLL_SECONDS) and applies
transitions to the ScraperControl singleton. Works without the running process's
cooperation being online (file-based), survives restarts.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Optional

import config


def _load(path=None) -> dict:
    path = path or config.CONTROL_FLAGS_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    config.ensure_state_dir()
    tmp = f"{config.CONTROL_FLAGS_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    import os

    os.replace(tmp, str(config.CONTROL_FLAGS_FILE))


def set_flag(name: str, value: bool) -> None:
    if name not in ("pause", "stop"):
        raise ValueError(f"unknown flag: {name}")
    data = _load()
    data[name] = value
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save(data)


def get_flags() -> dict:
    data = _load()
    return {"pause": bool(data.get("pause", False)), "stop": bool(data.get("stop", False)),
            "updated_at": data.get("updated_at")}


# ---- runtime side (used by main.py watchdog) ----

class FlagWatcher:
    """Applies control-flag transitions to a ScraperControl-like object."""

    def __init__(self) -> None:
        self._last: Optional[dict] = None

    def apply(self, control) -> dict:
        flags = get_flags()
        if flags == self._last:
            return flags
        prev = self._last or {"pause": False, "stop": False}
        if flags["stop"] and not prev.get("stop"):
            control.request_stop()
        elif flags["pause"] and not prev.get("pause"):
            control.pause("flags")
        elif prev.get("pause") and not flags["pause"]:
            control.resume()
        self._last = flags
        return flags


def _cli(argv: list[str]) -> int:
    if not argv or argv[0] not in ("pause", "resume", "stop", "status", "clear"):
        print(__doc__, file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "status":
        print(json.dumps(get_flags(), indent=2))
        return 0
    if cmd == "clear":
        set_flag("pause", False)
        set_flag("stop", False)
        print("flags cleared")
        return 0
    if cmd == "pause":
        set_flag("pause", True)
        print("pause flag set")
        return 0
    if cmd == "resume":
        set_flag("pause", False)
        print("pause flag cleared")
        return 0
    if cmd == "stop":
        set_flag("stop", True)
        print("stop flag set — the bot will shut down gracefully")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
