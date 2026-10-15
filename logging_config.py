"""Structured logging setup for telegram-rag-bot.

Console: human-readable lines. Rotating file: JSON lines (one object per record)
for machine parsing. `setup_logging()` is idempotent and called once from main.py;
every module logger lives under the "telegram_rag_bot" namespace.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import time
from pathlib import Path
from typing import Optional

import config

_ROOT_NAME = "telegram_rag_bot"
_configured = False


class JsonFormatter(logging.Formatter):
    """One JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def get_logger(name: str = "") -> logging.Logger:
    """Return a logger under the project namespace (does not configure handlers)."""
    suffix = name.strip(".")
    return logging.getLogger(f"{_ROOT_NAME}.{suffix}" if suffix else _ROOT_NAME)


def setup_logging(level: Optional[str] = None, json_console: Optional[bool] = None) -> Path:
    """Configure root handlers. Idempotent. Returns the log file path.

    - console handler: human format (level from config.LOG_LEVEL by default)
    - rotating file handler: JSON lines at logs/telegram-rag-bot.log
    """
    global _configured
    log_dir = Path(config.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "telegram-rag-bot.log"

    root = logging.getLogger(_ROOT_NAME)
    root.setLevel(getattr(logging, (level or config.LOG_LEVEL).upper(), logging.INFO))

    if _configured:
        return log_file

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    root.propagate = False
    _configured = True
    return log_file
