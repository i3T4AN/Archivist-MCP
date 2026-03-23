"""Sensitive data redaction helpers for logs and exports."""

from __future__ import annotations

import re
from typing import Any

_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[A-Za-z0-9._\-+/=]+"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(secret\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
)


def _redact_string(value: str) -> str:
    out = value
    out = _PATTERNS[0].sub(r"\1[REDACTED]", out)
    out = _PATTERNS[1].sub(r"\1[REDACTED]", out)
    out = _PATTERNS[2].sub(r"\1[REDACTED]", out)
    out = _PATTERNS[3].sub("[REDACTED_AWS_KEY]", out)
    out = _PATTERNS[4].sub("[REDACTED_TOKEN]", out)
    return out


def redact_sensitive(value: Any) -> Any:
    """Recursively redact sensitive patterns for safe logging/export."""
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, list):
        return [redact_sensitive(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(v) for v in value)
    if isinstance(value, dict):
        return {str(k): redact_sensitive(v) for k, v in value.items()}
    return value
