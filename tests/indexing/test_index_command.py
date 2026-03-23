from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class IndexCommandTests(unittest.TestCase):
    def test_index_command_outputs_performance_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            shutil.copytree(Path("tests/indexing/fixtures/repo_a"), repo)
            db_path = root / "archivist.db"

            proc = subprocess.run(
                [
                    sys.executable,
                    "scripts/index_symbols.py",
                    "--project-id",
                    "proj-1",
                    "--root",
                    str(repo),
                    "--db",
                    str(db_path),
                ],
                cwd=Path("."),
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Indexing Performance Report", proc.stdout)
            lines = [line for line in proc.stdout.splitlines() if line.strip()]
            payload = json.loads("\n".join(lines[1:]))
            self.assertEqual(payload["project_id"], "proj-1")
            self.assertGreater(payload["scanned_files"], 0)


if __name__ == "__main__":
    unittest.main()
