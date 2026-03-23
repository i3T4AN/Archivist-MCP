"""In-memory metrics collector for tool requests."""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any


class InMemoryMetrics:
    """Thread-safe request/error/latency counters."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tool_calls: dict[str, int] = defaultdict(int)
        self._error_codes: dict[str, int] = defaultdict(int)
        self._tool_latency_total_ms: dict[str, float] = defaultdict(float)
        self._tool_latency_samples: dict[str, int] = defaultdict(int)

    def record(self, tool: str, *, duration_ms: float, error_code: str | None = None) -> None:
        with self._lock:
            self._tool_calls[tool] += 1
            self._tool_latency_total_ms[tool] += max(0.0, duration_ms)
            self._tool_latency_samples[tool] += 1
            if error_code:
                self._error_codes[error_code] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            by_tool: list[dict[str, Any]] = []
            for tool in sorted(self._tool_calls.keys()):
                samples = self._tool_latency_samples.get(tool, 0)
                total = self._tool_latency_total_ms.get(tool, 0.0)
                by_tool.append(
                    {
                        "tool": tool,
                        "calls": self._tool_calls[tool],
                        "avg_latency_ms": round((total / samples) if samples else 0.0, 6),
                    }
                )
            errors = [{"code": code, "count": self._error_codes[code]} for code in sorted(self._error_codes)]
            return {
                "total_calls": sum(self._tool_calls.values()),
                "by_tool": by_tool,
                "errors": errors,
            }

    def render_prometheus(self) -> str:
        """Render metrics in Prometheus text exposition format."""
        snap = self.snapshot()
        lines: list[str] = [
            "# HELP archivist_total_calls Total tool calls handled by Archivist.",
            "# TYPE archivist_total_calls counter",
            f"archivist_total_calls {snap['total_calls']}",
            "# HELP archivist_tool_calls_total Tool call count by tool name.",
            "# TYPE archivist_tool_calls_total counter",
        ]
        for row in snap["by_tool"]:
            lines.append(f'archivist_tool_calls_total{{tool="{row["tool"]}"}} {row["calls"]}')
        lines.extend(
            [
                "# HELP archivist_tool_avg_latency_ms Average latency by tool in milliseconds.",
                "# TYPE archivist_tool_avg_latency_ms gauge",
            ]
        )
        for row in snap["by_tool"]:
            lines.append(
                f'archivist_tool_avg_latency_ms{{tool="{row["tool"]}"}} {row["avg_latency_ms"]}'
            )
        lines.extend(
            [
                "# HELP archivist_errors_total Error count by error code.",
                "# TYPE archivist_errors_total counter",
            ]
        )
        for row in snap["errors"]:
            lines.append(f'archivist_errors_total{{code="{row["code"]}"}} {row["count"]}')
        lines.append("")
        return "\n".join(lines)
