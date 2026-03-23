from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from archivist_mcp.db import connect
from archivist_mcp.memory.materializer import CoreMemoryMaterializer
from archivist_mcp.migrations.runner import run_migrations


class CoreMemoryMaterializerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "test.db"
        self.conn = connect(str(self.db_path))
        run_migrations(self.conn, Path("archivist_mcp/migrations/sql"))
        self.conn.execute("INSERT INTO projects(project_id, name) VALUES ('proj-1', 'Project')")
        self.conn.execute("INSERT INTO users(user_id, display_name) VALUES ('user-1', 'User')")
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tempdir.cleanup()

    def test_budget_enforcement_truncates_deterministically(self) -> None:
        for idx in range(8):
            self.conn.execute(
                """
                INSERT INTO nodes(node_id, project_id, type, title, content, state, created_by, updated_at)
                VALUES (?, 'proj-1', 'Decision', ?, ?, 'active', 'user-1', ?)
                """,
                (
                    f"d-{idx}",
                    f"Decision {idx}",
                    "x" * 600,
                    f"2026-01-01T00:00:0{idx}Z",
                ),
            )
        self.conn.commit()

        materializer = CoreMemoryMaterializer(self.conn, self.root, core_max_kb=2)
        out1 = materializer.refresh("proj-1")
        out2 = materializer.refresh("proj-1")

        json_path = self.root / "core_memory.json"
        size = len(json_path.read_bytes())
        self.assertLessEqual(size, 2048)
        self.assertTrue(out1["metadata"]["truncated"])
        self.assertEqual(out1, out2)

    def test_deterministic_output_ordering(self) -> None:
        self.conn.execute(
            """
            INSERT INTO nodes(node_id, project_id, type, title, content, state, created_by, updated_at)
            VALUES ('rule-b', 'proj-1', 'Rule', 'Rule B', 'b', 'active', 'user-1', '2026-01-02T00:00:00Z')
            """
        )
        self.conn.execute(
            """
            INSERT INTO nodes(node_id, project_id, type, title, content, state, created_by, updated_at)
            VALUES ('rule-a', 'proj-1', 'Rule', 'Rule A', 'a', 'active', 'user-1', '2026-01-02T00:00:00Z')
            """
        )
        self.conn.commit()

        materializer = CoreMemoryMaterializer(self.conn, self.root, core_max_kb=12)
        materializer.refresh("proj-1")
        first_md = (self.root / "core_memory.md").read_text(encoding="utf-8")
        first_json = (self.root / "core_memory.json").read_text(encoding="utf-8")

        materializer.refresh("proj-1")
        second_md = (self.root / "core_memory.md").read_text(encoding="utf-8")
        second_json = (self.root / "core_memory.json").read_text(encoding="utf-8")

        self.assertEqual(first_md, second_md)
        self.assertEqual(first_json, second_json)

    def test_missing_data_writes_empty_sections(self) -> None:
        materializer = CoreMemoryMaterializer(self.conn, self.root, core_max_kb=12)
        payload = materializer.refresh("proj-1")

        self.assertEqual(payload["sections"]["decisions"], [])
        self.assertEqual(payload["sections"]["rules"], [])
        self.assertEqual(payload["sections"]["high_priority_incidents"], [])
        self.assertEqual(payload["sections"]["architecture_map"], [])

        data = json.loads((self.root / "core_memory.json").read_text(encoding="utf-8"))
        self.assertEqual(data["project_id"], "proj-1")
        self.assertTrue((self.root / "core_memory.md").exists())


if __name__ == "__main__":
    unittest.main()
