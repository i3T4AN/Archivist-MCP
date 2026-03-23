from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class BenchmarkScriptTests(unittest.TestCase):
    def test_seed_and_benchmark_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "bench.db"
            queries = root / "queries.json"

            seed = subprocess.run(
                [
                    sys.executable,
                    "scripts/seed_retrieval_benchmark.py",
                    "--db",
                    str(db),
                    "--project-id",
                    "bench-proj",
                    "--out",
                    str(queries),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(seed.returncode, 0, seed.stderr)
            self.assertTrue(queries.exists())

            bench = subprocess.run(
                [
                    sys.executable,
                    "scripts/benchmark_retrieval.py",
                    "--db",
                    str(db),
                    "--query-file",
                    str(queries),
                    "--top-k",
                    "5",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(bench.returncode, 0, bench.stderr)
            self.assertIn("Retrieval Benchmark Report", bench.stdout)
            lines = [line for line in bench.stdout.splitlines() if line.strip()]
            payload = json.loads("\n".join(lines[1:]))
            self.assertIn("precision", payload)
            self.assertIn("recall", payload)
            self.assertIn("latency_ms", payload)


if __name__ == "__main__":
    unittest.main()
