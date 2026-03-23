#!/usr/bin/env python3
"""Export redacted audit events for compliance workflows."""

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
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--since")
    parser.add_argument("--out", default="-")
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
    resp = server.handle_tool(
        "export_audit_log",
        {
            "project_id": args.project_id,
            "limit": args.limit,
            **({"since": args.since} if args.since else {}),
        },
    )
    if "error" in resp:
        raise SystemExit(resp["error"]["message"])

    payload = resp["data"]
    out = json.dumps(payload, indent=2, sort_keys=True)
    if args.out == "-":
        print(out)
    else:
        Path(args.out).write_text(out, encoding="utf-8")


if __name__ == "__main__":
    main()
