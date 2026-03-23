"""Input hardening utilities for tool payloads."""

from __future__ import annotations

import re
import unicodedata

_INJECTION_PATTERNS = (
    re.compile(r"(?i)\bignore\s+previous\s+instructions?\b"),
    re.compile(r"(?i)\bdisregard\s+above\b"),
    re.compile(r"(?i)\bsystem\s+prompt\b"),
    re.compile(r"(?i)\bdeveloper\s+message\b"),
)


def normalize_text(value: str) -> str:
    """Normalize string input for consistent policy checks and storage."""
    norm = unicodedata.normalize("NFKC", value)
    out: list[str] = []
    for ch in norm:
        if ch in ("\n", "\r", "\t"):
            out.append(ch)
            continue
        cat = unicodedata.category(ch)
        if cat.startswith("C"):
            continue
        out.append(ch)
    return "".join(out).strip()


def sanitize_text(value: str) -> tuple[str, bool]:
    """Reduce prompt/data injection risk in free-form text fields."""
    text = normalize_text(value)
    changed = text != value
    for pattern in _INJECTION_PATTERNS:
        redacted, count = pattern.subn("[filtered]", text)
        if count:
            changed = True
            text = redacted
    return text, changed
