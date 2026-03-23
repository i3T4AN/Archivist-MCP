#!/usr/bin/env python3
"""Rebuild symbol index and embeddings for a project."""

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
from archivist_mcp.retrieval.embeddings import EmbeddingConfig as WorkerEmbeddingConfig
from archivist_mcp.retrieval.embeddings import EmbeddingWorker
from archivist_mcp.tooling.server import ToolServer


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=".archivist/archivist.db")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--root", required=True)
    args = parser.parse_args()

    config = load_config()
    conn = connect(
        args.db,
        encryption_key=config.security.encryption_key,
        encryption_required=config.security.encryption_required,
    )
    run_migrations(conn, MIGRATIONS_DIR)
    embed = EmbeddingWorker(
        conn,
        WorkerEmbeddingConfig(
            enabled=config.embedding.enabled,
            provider=config.embedding.provider,
            model=config.embedding.model,
            dimensions=config.embedding.dimensions,
            offline_strict=config.embedding.offline_strict,
        ),
    )
    server = ToolServer(
        conn,
        embedding_worker=embed,
        security=config.security,
        enable_experimental_tools=config.feature_flags.experimental_tools_enabled,
        disabled_tools=set(config.feature_flags.disabled_tools),
    )
    result = server.handle_tool(
        "rebuild_index_and_embeddings",
        {"project_id": args.project_id, "root_path": str(Path(args.root).resolve())},
    )
    if "error" in result:
        raise SystemExit(result["error"]["message"])
    print(json.dumps(result["data"], indent=2, sort_keys=True))
    if result["warnings"]:
        print(json.dumps({"warnings": result["warnings"]}))


if __name__ == "__main__":
    main()
