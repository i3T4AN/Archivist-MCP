#!/usr/bin/env python3
"""Run retrieval benchmark and print quality/latency metrics."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archivist_mcp.db import connect
from archivist_mcp.retrieval.embeddings import EmbeddingConfig, EmbeddingWorker
from archivist_mcp.retrieval.hybrid import HybridRetrievalEngine, RetrievalWeights


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(round((p / 100.0) * (len(ordered) - 1)))
    return ordered[idx]


def run_benchmark(
    *,
    db_path: str,
    query_file: str,
    disable_embeddings: bool,
    top_k: int,
) -> dict:
    payload = json.loads(Path(query_file).read_text(encoding="utf-8"))
    project_id = payload["project_id"]
    queries = payload["queries"]

    conn = connect(db_path)
    embed = EmbeddingWorker(
        conn,
        EmbeddingConfig(
            enabled=not disable_embeddings,
            provider="hash-local",
            model="bge-small-en-v1.5",
            dimensions=384,
            offline_strict=True,
        ),
    )
    engine = HybridRetrievalEngine(conn, embed, RetrievalWeights())

    latencies: list[float] = []
    precision_values: list[float] = []
    recall_values: list[float] = []
    warnings_seen: set[str] = set()

    for q in queries:
        start = time.perf_counter()
        result = engine.search(project_id=project_id, query=q["query"], limit=top_k)
        latencies.append((time.perf_counter() - start) * 1000.0)
        warnings_seen.update(result.get("warnings", []))

        predicted = [r["node_id"] for r in result["results"]]
        relevant = set(q["relevant"])
        hit = len([p for p in predicted if p in relevant])

        precision = hit / max(1, len(predicted))
        recall = hit / max(1, len(relevant))
        precision_values.append(precision)
        recall_values.append(recall)

    report = {
        "queries": len(queries),
        "precision": round(statistics.mean(precision_values), 6),
        "recall": round(statistics.mean(recall_values), 6),
        "latency_ms": {
            "p50": round(percentile(latencies, 50), 3),
            "p95": round(percentile(latencies, 95), 3),
            "avg": round(statistics.mean(latencies), 3),
        },
        "warnings": sorted(warnings_seen),
    }
    conn.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--query-file", required=True)
    parser.add_argument("--disable-embeddings", action="store_true")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--json-out")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    report = run_benchmark(
        db_path=args.db,
        query_file=args.query_file,
        disable_embeddings=args.disable_embeddings,
        top_k=args.top_k,
    )
    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    if not args.quiet:
        print("Retrieval Benchmark Report")
        print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
