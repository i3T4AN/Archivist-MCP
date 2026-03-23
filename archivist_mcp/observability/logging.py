"""Structured logging utilities with trace-aware JSON events."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any


def setup_structured_logger(name: str = "archivist") -> logging.Logger:
    """Configure a logger that writes JSON lines to stderr."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def log_event(logger: logging.Logger | None, event: str, **fields: Any) -> None:
    """Write a single JSON log line, silently no-op when logger is disabled."""
    if logger is None:
        return
    payload: dict[str, Any] = {"event": event}
    payload.update(fields)
    logger.info(json.dumps(payload, sort_keys=True, ensure_ascii=False))

