"""Strukturierte Logs: ein Ereignis pro Zeile, wahlweise logfmt oder JSON.

Benutzung::

    log = get_logger(__name__)
    log.info("mount.ok", extra={"fields": {"mount": "scans", "took_ms": 120}})

oder kürzer über :func:`event`::

    event(log, "mount.ok", mount="scans", took_ms=120)
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_RESERVED = {"ts", "level", "logger", "event"}


def _logfmt_value(value: Any) -> str:
    text = str(value)
    if text == "" or any(c in text for c in ' ="\n\t'):
        text = '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return text


class StructuredFormatter(logging.Formatter):
    def __init__(self, fmt: str = "logfmt") -> None:
        super().__init__()
        self.fmt = fmt

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            for key, value in fields.items():
                data[f"f_{key}" if key in _RESERVED else key] = value
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        if self.fmt == "json":
            return json.dumps(data, default=str, ensure_ascii=False)
        return " ".join(f"{k}={_logfmt_value(v)}" for k, v in data.items())


def setup_logging(level: str = "INFO", fmt: str = "logfmt") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(StructuredFormatter(fmt))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Uvicorn bringt eigene Handler mit; wir wollen ein einheitliches Format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "apscheduler"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def event(logger: logging.Logger, name: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, name, extra={"fields": fields})
