"""Structured logging on top of the standard library (no extra dependency).

Usage:
    setup_logging("INFO", "json")
    log = get_logger(__name__, run_id=run_id, stage="clean")
    log.info("document written", extra={"event": "doc.written", "doc_id": d.doc_id})
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")}
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.name}: {record.getMessage()}"
        if extras:
            base += "  " + " ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def setup_logging(level: str = "INFO", fmt: str = "json") -> None:
    import time

    handler = logging.StreamHandler(sys.stderr)
    formatter: logging.Formatter = JsonFormatter() if fmt == "json" else TextFormatter()
    formatter.converter = time.gmtime  # UTC timestamps
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "urllib3", "chromadb", "sentence_transformers", "filelock", "trafilatura"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class _ContextAdapter(logging.LoggerAdapter):
    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        kwargs["extra"] = {**(self.extra or {}), **kwargs.get("extra", {})}
        return msg, kwargs


def get_logger(name: str, **context: Any) -> logging.LoggerAdapter:
    """Logger that stamps `context` (e.g. run_id, stage) on every record."""
    return _ContextAdapter(logging.getLogger(name), context)
