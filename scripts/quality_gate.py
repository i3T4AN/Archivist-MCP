#!/usr/bin/env python3
"""Quality gate pipeline for retrieval, latency, integration, and security."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_retrieval import run_benchmark


@dataclass
class GateThresholds:
    min_precision: float = 0.35
    min_recall: float = 0.35
    max_p95_ms: float = 250.0


@dataclass
class GateResult:
    passed: bool
    retrieval_passed: bool
    latency_passed: bool
    integration_passed: bool
    security_passed: bool
    reliability_passed: bool
    thresholds: dict
    retrieval: dict
    retrieval_no_embeddings: dict
    commands: dict


def run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def ensure_benchmark_seed(db: str, project_id: str, query_file: str) -> None:
    cmd = [
        sys.executable,
        "scripts/seed_retrieval_benchmark.py",
        "--db",
        db,
        "--project-id",
        project_id,
        "--out",
        query_file,
    ]
    code, stdout, stderr = run_cmd(cmd)
    if code != 0:
        raise RuntimeError(f"seed benchmark failed: {stderr or stdout}")


def append_history(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=".archivist/archivist.db")
    parser.add_argument("--project-id", default="proj-1")
    parser.add_argument("--query-file", default=".archivist/benchmark_queries.json")
    parser.add_argument("--history", default=".archivist/benchmark_history.jsonl")
    parser.add_argument("--report-out", default=".archivist/reports/quality_gate_latest.json")
    parser.add_argument("--min-precision", type=float, default=0.35)
    parser.add_argument("--min-recall", type=float, default=0.35)
    parser.add_argument("--max-p95-ms", type=float, default=250.0)
    args = parser.parse_args()

    thresholds = GateThresholds(
        min_precision=args.min_precision,
        min_recall=args.min_recall,
        max_p95_ms=args.max_p95_ms,
    )

    ensure_benchmark_seed(args.db, args.project_id, args.query_file)
    retrieval = run_benchmark(
        db_path=args.db,
        query_file=args.query_file,
        disable_embeddings=False,
        top_k=5,
    )
    retrieval_no_embeddings = run_benchmark(
        db_path=args.db,
        query_file=args.query_file,
        disable_embeddings=True,
        top_k=5,
    )

    integration_cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests/integration", "-v"]
    security_cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests/security", "-v"]
    reliability_cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests/reliability", "-v"]

    integration_code, integration_out, integration_err = run_cmd(integration_cmd)
    security_code, security_out, security_err = run_cmd(security_cmd)
    reliability_code, reliability_out, reliability_err = run_cmd(reliability_cmd)

    retrieval_passed = retrieval["precision"] >= thresholds.min_precision and retrieval["recall"] >= thresholds.min_recall
    latency_passed = retrieval["latency_ms"]["p95"] <= thresholds.max_p95_ms
    integration_passed = integration_code == 0
    security_passed = security_code == 0
    reliability_passed = reliability_code == 0

    passed = all([retrieval_passed, latency_passed, integration_passed, security_passed, reliability_passed])

    result = GateResult(
        passed=passed,
        retrieval_passed=retrieval_passed,
        latency_passed=latency_passed,
        integration_passed=integration_passed,
        security_passed=security_passed,
        reliability_passed=reliability_passed,
        thresholds=asdict(thresholds),
        retrieval=retrieval,
        retrieval_no_embeddings=retrieval_no_embeddings,
        commands={
            "integration": {"cmd": integration_cmd, "exit_code": integration_code, "stderr": integration_err.strip()},
            "security": {"cmd": security_cmd, "exit_code": security_code, "stderr": security_err.strip()},
            "reliability": {"cmd": reliability_cmd, "exit_code": reliability_code, "stderr": reliability_err.strip()},
        },
    )

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "git_sha": os.getenv("GITHUB_SHA", "local"),
        "result": asdict(result),
    }

    report_path = Path(args.report_out)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    append_history(Path(args.history), report)

    print("Quality Gate Report")
    print(json.dumps(report, indent=2, sort_keys=True))

    if not integration_passed:
        print(integration_out)
    if not security_passed:
        print(security_out)
    if not reliability_passed:
        print(reliability_out)

    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
