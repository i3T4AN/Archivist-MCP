#!/usr/bin/env python3
"""Seed benchmark dataset for retrieval quality and latency."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archivist_mcp.db import connect
from archivist_mcp.migrations.runner import run_migrations
from archivist_mcp.retrieval.embeddings import EmbeddingConfig, EmbeddingWorker


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=".archivist/archivist.db")
    parser.add_argument("--project-id", default="proj-1")
    parser.add_argument("--out", default=".archivist/benchmark_queries.json")
    args = parser.parse_args()

    conn = connect(args.db)
    run_migrations(conn, ROOT / "archivist_mcp/migrations/sql")
    conn.execute(
        "INSERT INTO projects(project_id, name) VALUES (?, ?) ON CONFLICT(project_id) DO NOTHING",
        (args.project_id, "Benchmark Project"),
    )

    dataset = [
        ("n-decision-sqlite", "Decision", "Use SQLite WAL", "Decision to use SQLite WAL mode for local concurrency"),
        ("n-rule-tests", "Rule", "Require Tests", "All changes must include unit tests and validation evidence"),
        ("n-incident-timeout", "Incident", "Timeout Incident", "API timed out under load and was fixed with pooled connections"),
        ("n-entity-indexer", "Entity", "Indexer", "Incremental symbol indexer parses changed files only"),
        ("n-entity-retrieval", "Entity", "RetrievalEngine", "Hybrid retrieval fuses FTS vector graph and recency signals"),
    ]

    for node_id, ntype, title, content in dataset:
        conn.execute(
            """
            INSERT INTO nodes(node_id, project_id, type, title, content, state)
            VALUES (?, ?, ?, ?, ?, 'active')
            ON CONFLICT(node_id) DO UPDATE SET title=excluded.title, content=excluded.content
            """,
            (node_id, args.project_id, ntype, title, content),
        )

    conn.execute(
        """
        INSERT INTO edges(edge_id, project_id, type, from_node_id, to_node_id, state)
        VALUES ('e1', ?, 'DEPENDS_ON', 'n-entity-retrieval', 'n-entity-indexer', 'active')
        ON CONFLICT(edge_id) DO NOTHING
        """,
        (args.project_id,),
    )
    conn.commit()

    embed = EmbeddingWorker(conn, EmbeddingConfig(enabled=True, provider="hash-local", model="bge-small-en-v1.5", dimensions=384, offline_strict=True))
    embed.rebuild_node_embeddings(args.project_id)

    queries = [
        {"query": "sqlite wal concurrency", "relevant": ["n-decision-sqlite"]},
        {"query": "test policy", "relevant": ["n-rule-tests"]},
        {"query": "timeout load incident", "relevant": ["n-incident-timeout"]},
        {"query": "incremental symbol parsing", "relevant": ["n-entity-indexer"]},
        {"query": "hybrid retrieval scoring", "relevant": ["n-entity-retrieval"]},
    ]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"project_id": args.project_id, "queries": queries}, indent=2), encoding="utf-8")
    conn.close()
    print(f"Seeded retrieval benchmark dataset into {args.db}")
    print(f"Wrote benchmark query set to {out}")


if __name__ == "__main__":
    main()
