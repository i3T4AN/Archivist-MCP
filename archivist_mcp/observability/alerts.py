"""Alert pipeline for runtime error-rate monitoring."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AlertConfig:
    enabled: bool = True
    min_calls: int = 20
    error_rate_threshold: float = 0.5
    cooldown_seconds: int = 60


class AlertPipeline:
    """Evaluates recent call outcomes and emits threshold alerts."""

    def __init__(self, config: AlertConfig):
        self.config = config
        self._events: deque[tuple[float, bool]] = deque(maxlen=max(config.min_calls * 10, 100))
        self._last_alert_at = 0.0

    def record(self, *, error: bool, now: float | None = None) -> dict[str, Any] | None:
        if not self.config.enabled:
            return None
        t = time.time() if now is None else now
        self._events.append((t, error))

        total = len(self._events)
        if total < self.config.min_calls:
            return None
        errors = sum(1 for _, is_error in self._events if is_error)
        rate = errors / total
        if rate < self.config.error_rate_threshold:
            return None
        if t - self._last_alert_at < self.config.cooldown_seconds:
            return None

        self._last_alert_at = t
        return {
            "window_calls": total,
            "window_errors": errors,
            "error_rate": round(rate, 6),
            "threshold": self.config.error_rate_threshold,
        }

