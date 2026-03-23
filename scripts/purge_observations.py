#!/usr/bin/env python3
"""Run observation retention purge job."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archivist_mcp.config import load_config
from archivist_mcp.db import connect
from archivist_mcp.migrations.runner import run_migrations
from archivist_mcp.paths import MIGRATIONS_DIR
from archivist_mcp.tooling.server import ToolServer


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=".archivist/archivist.db")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--retention-days", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_config()
    conn = connect(
        args.db,
        encryption_key=config.security.encryption_key,
        encryption_required=config.security.encryption_required,
    )
    run_migrations(conn, MIGRATIONS_DIR)
    server = ToolServer(
        conn,
        security=config.security,
        enable_experimental_tools=config.feature_flags.experimental_tools_enabled,
        disabled_tools=set(config.feature_flags.disabled_tools),
    )
    payload = {"project_id": args.project_id, "dry_run": args.dry_run}
    if args.retention_days is not None:
        payload["retention_days"] = args.retention_days
    resp = server.handle_tool("purge_observations", payload)
    if "error" in resp:
        raise SystemExit(resp["error"]["message"])
    print(json.dumps(resp["data"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
