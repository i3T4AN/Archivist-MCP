#!/usr/bin/env python3
"""Run Archivist DB migrations."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

                                                              
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archivist_mcp.db import connect
from archivist_mcp.config import load_config
from archivist_mcp.migrations.runner import run_migrations


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=".archivist/archivist.db")
    args = parser.parse_args()

    db_path = Path(args.db)
    config = load_config()
    conn = connect(
        str(db_path),
        encryption_key=config.security.encryption_key,
        encryption_required=config.security.encryption_required,
    )
    applied = run_migrations(conn, ROOT / "archivist_mcp/migrations/sql")
    conn.close()
    print(f"Migrated database at {db_path}")
    if applied:
        print(f"Applied versions: {', '.join(applied)}")
    else:
        print('No pending migrations')


if __name__ == '__main__':
    main()
