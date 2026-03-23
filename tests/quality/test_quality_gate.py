from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class QualityGateScriptTests(unittest.TestCase):
    def test_quality_gate_writes_report_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "qg.db"
            query_file = root / "queries.json"
            history = root / "history.jsonl"
            report = root / "report.json"

            proc = subprocess.run(
                [
                    sys.executable,
                    "scripts/quality_gate.py",
                    "--db",
                    str(db),
                    "--project-id",
                    "bench-proj",
                    "--query-file",
                    str(query_file),
                    "--history",
                    str(history),
                    "--report-out",
                    str(report),
                    "--min-precision",
                    "0.1",
                    "--min-recall",
                    "0.1",
                    "--max-p95-ms",
                    "9999",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(report.exists())
            self.assertTrue(history.exists())

            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertIn("result", payload)
            self.assertTrue(payload["result"]["passed"])

            lines = [line for line in history.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertGreaterEqual(len(lines), 1)

    def test_quality_gate_fails_when_thresholds_too_strict(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "qg_fail.db"
            query_file = root / "queries.json"
            history = root / "history.jsonl"
            report = root / "report.json"

            proc = subprocess.run(
                [
                    sys.executable,
                    "scripts/quality_gate.py",
                    "--db",
                    str(db),
                    "--project-id",
                    "bench-proj",
                    "--query-file",
                    str(query_file),
                    "--history",
                    str(history),
                    "--report-out",
                    str(report),
                    "--min-precision",
                    "0.99",
                    "--min-recall",
                    "0.99",
                    "--max-p95-ms",
                    "1",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertFalse(payload["result"]["passed"])


if __name__ == "__main__":
    unittest.main()
