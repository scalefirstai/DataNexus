"""Structured JSON logging (§9.1)."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_ctx: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("log_ctx", default={})


def set_log_context(**fields: Any) -> None:
    merged = {**_ctx.get(), **fields}
    _ctx.set({k: v for k, v in merged.items() if v is not None})


def clear_log_context() -> None:
    _ctx.set({})


def get_log_context() -> dict[str, Any]:
    return dict(_ctx.get())


MANDATORY_FIELDS = (
    "timestamp",
    "level",
    "service",
    "extract_id",
    "run_id",
    "app_id",
    "correlation_id",
    "fund_id",
    "duration_ms",
    "message",
)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        ctx = _ctx.get()
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "message": record.getMessage(),
        }
        for k in ("extract_id", "run_id", "app_id", "correlation_id", "fund_id", "duration_ms"):
            v = ctx.get(k) or getattr(record, k, None)
            if v is not None:
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for k, v in record.__dict__.items():
            if (
                k
                in (
                    "name",
                    "msg",
                    "args",
                    "levelname",
                    "levelno",
                    "pathname",
                    "filename",
                    "module",
                    "exc_info",
                    "exc_text",
                    "stack_info",
                    "lineno",
                    "funcName",
                    "created",
                    "msecs",
                    "relativeCreated",
                    "thread",
                    "threadName",
                    "processName",
                    "process",
                    "message",
                    "taskName",
                )
                or k in payload
            ):
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except TypeError:
                payload[k] = str(v)
        return json.dumps(payload, default=str)


def configure_logging(service: str, level: str = "INFO") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root.addHandler(handler)
    root.setLevel(level)
