"""Migration runner for Archivist schema."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _ensure_migration_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        """
    )


def run_migrations(conn: sqlite3.Connection, migration_dir: Path) -> list[str]:
    """Apply unapplied SQL migrations from migration_dir in filename order."""
    _ensure_migration_table(conn)
    applied = {
        row["version"]
        for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
    }

    applied_now: list[str] = []
    sql_files = sorted(migration_dir.glob("*.sql"))

    for sql_file in sql_files:
        version = sql_file.name.split("_", 1)[0]
        if version in applied:
            continue

        sql = sql_file.read_text(encoding="utf-8")
        with conn:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                (version,),
            )
        applied_now.append(version)

    return applied_now
