"""Simple in-memory rate limiter."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    """Sliding-window per-actor, per-tool limiter."""

    def __init__(self, *, enabled: bool, per_actor_per_minute: int):
        self.enabled = enabled
        self.per_actor_per_minute = max(1, per_actor_per_minute)
        self._lock = threading.Lock()
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def allow(self, *, actor: str, tool: str, now: float | None = None) -> bool:
        if not self.enabled:
            return True
        t = time.time() if now is None else now
        cutoff = t - 60.0
        key = (actor, tool)
        with self._lock:
            q = self._events[key]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self.per_actor_per_minute:
                return False
            q.append(t)
            return True

