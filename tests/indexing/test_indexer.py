from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from archivist_mcp.db import connect
from archivist_mcp.indexing.indexer import SymbolIndexer
from archivist_mcp.migrations.runner import run_migrations


class SymbolIndexerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "repo"
        shutil.copytree(Path("tests/indexing/fixtures/repo_a"), self.repo)

        self.db = self.root / "archivist.db"
        self.conn = connect(str(self.db))
        run_migrations(self.conn, Path("archivist_mcp/migrations/sql"))
        self.conn.execute("INSERT INTO projects(project_id, name) VALUES ('proj-1', 'Project')")
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tempdir.cleanup()

    def test_add_update_delete_symbol_scenarios(self) -> None:
        indexer = SymbolIndexer(self.conn)
        report1 = indexer.index_project("proj-1", self.repo)
        self.assertGreater(report1.symbols_added_or_updated, 0)

        count_initial = self.conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE project_id='proj-1' AND type='Entity' AND state='active'"
        ).fetchone()[0]
        self.assertGreater(count_initial, 0)

        py_file = self.repo / "src/main.py"
        py_file.write_text(
            "import os\n\nclass Runner:\n    pass\n\ndef run(value):\n    return str(value)\n\ndef added(name):\n    return name\n",
            encoding="utf-8",
        )

        report2 = indexer.index_project("proj-1", self.repo)
        self.assertGreater(report2.symbols_added_or_updated, 0)

        py_file.write_text("import os\n\nclass Runner:\n    pass\n", encoding="utf-8")
        report3 = indexer.index_project("proj-1", self.repo)
        self.assertGreaterEqual(report3.symbols_deprecated, 1)

        deprecated = self.conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE project_id='proj-1' AND type='Entity' AND state='deprecated'"
        ).fetchone()[0]
        self.assertGreaterEqual(deprecated, 1)

    def test_incremental_skips_unchanged_files(self) -> None:
        indexer = SymbolIndexer(self.conn)
        first = indexer.index_project("proj-1", self.repo, incremental=True)
        second = indexer.index_project("proj-1", self.repo, incremental=True)

        self.assertGreater(first.changed_files, 0)
        self.assertEqual(second.changed_files, 0)

    def test_persists_provenance_metadata(self) -> None:
        indexer = SymbolIndexer(self.conn)
        indexer.index_project("proj-1", self.repo)

        row = self.conn.execute(
            """
            SELECT p.key, p.value_json
            FROM node_properties p
            JOIN nodes n ON n.node_id = p.node_id
            WHERE n.project_id='proj-1' AND p.key IN ('symbol_file_path','symbol_language','symbol_backend')
            LIMIT 1
            """
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn(row["key"], {"symbol_file_path", "symbol_language", "symbol_backend"})
        self.assertIsNotNone(json.loads(row["value_json"]))

    def test_removed_file_deprecates_symbols(self) -> None:
        indexer = SymbolIndexer(self.conn)
        indexer.index_project("proj-1", self.repo)
        (self.repo / "src/service.go").unlink()
        report = indexer.index_project("proj-1", self.repo)
        self.assertGreaterEqual(report.symbols_deprecated, 1)


if __name__ == "__main__":
    unittest.main()
