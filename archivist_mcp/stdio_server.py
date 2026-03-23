"""Minimal stdio transport for Archivist tool calls."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from archivist_mcp.config import load_config
from archivist_mcp.db import connect
from archivist_mcp.memory.materializer import CoreMemoryMaterializer
from archivist_mcp.migrations.runner import run_migrations
from archivist_mcp.paths import MIGRATIONS_DIR
from archivist_mcp.retrieval.embeddings import EmbeddingConfig as WorkerEmbeddingConfig
from archivist_mcp.retrieval.embeddings import EmbeddingWorker
from archivist_mcp.retrieval.hybrid import HybridRetrievalEngine, RetrievalWeights
from archivist_mcp.reliability.recovery import recover_database_on_startup
from archivist_mcp.security.redaction import redact_sensitive
from archivist_mcp.observability.alerts import AlertConfig, AlertPipeline
from archivist_mcp.observability.logging import setup_structured_logger
from archivist_mcp.observability.rate_limit import RateLimiter
from archivist_mcp.tooling.server import ToolServer


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=".archivist/archivist.db")
    parser.add_argument("--require-user-id", action="store_true")
    args = parser.parse_args()
    config = load_config()
    db_path = Path(args.db)
    if config.reliability.startup_integrity_check and db_path.exists():
        recover_database_on_startup(
            db_path,
            Path(config.reliability.snapshot_dir),
            config.reliability.auto_restore_on_corruption,
            encryption_key=config.security.encryption_key,
        )

    conn = connect(
        str(db_path),
        encryption_key=config.security.encryption_key,
        encryption_required=config.security.encryption_required,
    )
    run_migrations(conn, MIGRATIONS_DIR)
    core_dir = Path(args.db).resolve().parent
    materializer = CoreMemoryMaterializer(
        conn,
        output_dir=core_dir,
        core_max_kb=config.memory.core_max_kb,
    )
    embedding_worker = EmbeddingWorker(
        conn,
        WorkerEmbeddingConfig(
            enabled=config.embedding.enabled,
            provider=config.embedding.provider,
            model=config.embedding.model,
            dimensions=config.embedding.dimensions,
            offline_strict=config.embedding.offline_strict,
        ),
    )
    retrieval_engine = HybridRetrievalEngine(
        conn,
        embedding_worker,
        RetrievalWeights(
            fts_weight=config.retrieval.fts_weight,
            vector_weight=config.retrieval.vector_weight,
            graph_weight=config.retrieval.graph_weight,
            recency_weight=config.retrieval.recency_weight,
        ),
    )
    server = ToolServer(
        conn,
        require_user_id=args.require_user_id,
        core_materializer=materializer,
        embedding_worker=embedding_worker,
        retrieval_engine=retrieval_engine,
        security=config.security,
        rate_limiter=RateLimiter(
            enabled=config.rate_limit.enabled,
            per_actor_per_minute=config.rate_limit.per_actor_per_minute,
        ),
        alert_pipeline=AlertPipeline(
            AlertConfig(
                enabled=config.observability.alert_enabled,
                min_calls=config.observability.alert_min_calls,
                error_rate_threshold=config.observability.alert_error_rate_threshold,
                cooldown_seconds=config.observability.alert_cooldown_seconds,
            )
        ),
        logger=setup_structured_logger("archivist.stdio")
        if config.observability.structured_logging
        else None,
        enable_experimental_tools=config.feature_flags.experimental_tools_enabled,
        disabled_tools=set(config.feature_flags.disabled_tools),
    )

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            req_id = request.get("id")
            tool = request.get("tool")
            payload = request.get("args") or {}
            trace_id = request.get("trace_id")
            response = server.handle_tool(tool, payload, trace_id=trace_id)
            out = {"id": req_id, "result": response}
        except Exception as exc:                    
            out = {
                "id": None,
                "result": {
                    "trace_id": "stdio-error",
                    "version": 1,
                    "warnings": [],
                    "error": {
                        "code": "INTERNAL_STORAGE_ERROR",
                        "message": str(redact_sensitive(str(exc))),
                        "details": {},
                    },
                },
            }
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()

    conn.close()


if __name__ == "__main__":
    main()
