from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from archivist_mcp.db import connect
from archivist_mcp.migrations.runner import run_migrations
from archivist_mcp.tooling.server import ToolServer


class ToolFeatureFlagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "feature-flags.db"
        self.conn = connect(str(self.db))
        run_migrations(self.conn, Path("archivist_mcp/migrations/sql"))

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_capabilities_hide_disabled_and_experimental_tools(self) -> None:
        server = ToolServer(
            self.conn,
            enable_experimental_tools=False,
            disabled_tools={"search_graph"},
        )
        out = server.handle_tool("get_capabilities", {})
        self.assertIn("data", out)
        tools = out["data"]["tools"]
        self.assertNotIn("extract_symbols", tools)
        self.assertNotIn("search_graph", tools)
        self.assertIn("health", tools)

    def test_calling_disabled_tool_returns_validation_error(self) -> None:
        server = ToolServer(self.conn, disabled_tools={"search_graph"})
        out = server.handle_tool("search_graph", {"project_id": "p1", "query": "q"})
        self.assertIn("error", out)
        self.assertEqual(out["error"]["code"], "VALIDATION_ERROR")
        self.assertIn("tool disabled: search_graph", out["error"]["message"])


if __name__ == "__main__":
    unittest.main()
