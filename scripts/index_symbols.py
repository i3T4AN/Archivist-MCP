#!/usr/bin/env python3
"""Incremental symbol indexing command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archivist_mcp.db import connect
from archivist_mcp.indexing.indexer import SymbolIndexer
from archivist_mcp.migrations.runner import run_migrations


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--db", default=".archivist/archivist.db")
    parser.add_argument("--full", action="store_true", help="Disable incremental mode")
    args = parser.parse_args()

    conn = connect(args.db)
    run_migrations(conn, ROOT / "archivist_mcp/migrations/sql")
    conn.execute(
        "INSERT INTO projects(project_id, name) VALUES (?, ?) ON CONFLICT(project_id) DO NOTHING",
        (args.project_id, args.project_id),
    )
    conn.commit()
    indexer = SymbolIndexer(conn)

    report = indexer.index_project(
        project_id=args.project_id,
        root=Path(args.root).resolve(),
        incremental=not args.full,
    )
    conn.close()

    print("Indexing Performance Report")
    print(json.dumps({
        "project_id": report.project_id,
        "scanned_files": report.scanned_files,
        "changed_files": report.changed_files,
        "symbols_added_or_updated": report.symbols_added_or_updated,
        "symbols_deprecated": report.symbols_deprecated,
        "dependencies_created": report.dependencies_created,
        "duration_ms": report.duration_ms,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
