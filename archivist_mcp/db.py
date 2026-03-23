"""SQLite connection helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _quote_pragma(value: str) -> str:
    return value.replace("'", "''")


def connect(
    db_path: str,
    *,
    encryption_key: str | None = None,
    encryption_required: bool = False,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Open a SQLite connection configured for Archivist use."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    if encryption_key:
        conn.execute(f"PRAGMA key = '{_quote_pragma(encryption_key)}';")
    cipher_row = conn.execute("PRAGMA cipher_version;").fetchone()
    cipher_supported = bool(cipher_row and cipher_row[0])
    if encryption_key and not cipher_supported:
        conn.close()
        raise RuntimeError(
            "ARCHIVIST_DB_ENCRYPTION_KEY is set but SQLCipher is not available in this SQLite build"
        )
    if encryption_required and not cipher_supported:
        conn.close()
        raise RuntimeError(
            "Encryption required but SQLCipher is not available in this SQLite build"
        )
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = FULL;")
    conn.execute("PRAGMA wal_autocheckpoint = 1000;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA secure_delete = ON;")
    return conn
