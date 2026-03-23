from __future__ import annotations

import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path

from archivist_mcp.db import connect
from archivist_mcp.mcp_http_server import McpHttpApp, metrics_access_status, process_jsonrpc_message
from archivist_mcp.migrations.runner import run_migrations
from archivist_mcp.paths import MIGRATIONS_DIR
from archivist_mcp.team.auth import AuthContext
from archivist_mcp.tooling.server import ToolServer


class McpHttpProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "mcp-http.db"
        self.conn = connect(str(self.db_path))
        run_migrations(self.conn, MIGRATIONS_DIR)
        self.conn.execute("INSERT INTO projects(project_id, name) VALUES ('proj-1', 'Project One')")
        self.conn.execute("INSERT INTO users(user_id, display_name) VALUES ('user-1', 'User One')")
        self.conn.execute("INSERT INTO users(user_id, display_name) VALUES ('maint-1', 'Maintainer One')")
        self.conn.commit()
        self.server = ToolServer(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tempdir.cleanup()

    def test_initialize_list_call_and_metrics(self) -> None:
        app = McpHttpApp(self.server, tokens={})

        status, init = process_jsonrpc_message(
            app,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
            },
        )
        self.assertEqual(status, HTTPStatus.OK)
        assert init is not None
        self.assertIn("serverInfo", init["result"])

        status, tools = process_jsonrpc_message(app, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        self.assertEqual(status, HTTPStatus.OK)
        assert tools is not None
        names = {row["name"] for row in tools["result"]["tools"]}
        self.assertIn("health", names)

        status, created = process_jsonrpc_message(
            app,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "create_entity",
                    "arguments": {
                        "project_id": "proj-1",
                        "type": "Entity",
                        "title": "Parser",
                        "content": "Parses source code",
                        "user_id": "user-1",
                    },
                },
            },
        )
        self.assertEqual(status, HTTPStatus.OK)
        assert created is not None
        self.assertFalse(created["result"]["isError"])
        self.assertEqual(created["result"]["structuredContent"]["node"]["title"], "Parser")

        metrics_text = self.server.metrics.render_prometheus()
        self.assertIn("archivist_total_calls", metrics_text)

    def test_auth_enforcement_role_filter_and_metrics_scope(self) -> None:
        writer = AuthContext(user_id="user-1", role="writer", project_ids=("proj-1",))
        maint = AuthContext(user_id="maint-1", role="maintainer", project_ids=("proj-1",))
        app = McpHttpApp(self.server, tokens={"writer-token": writer, "maint-token": maint})

        status, unauthorized = process_jsonrpc_message(
            app, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
        assert unauthorized is not None
        self.assertEqual(unauthorized["error"]["code"], -32001)

        status, writer_tools = process_jsonrpc_message(
            app,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers={"Authorization": "Bearer writer-token"},
        )
        self.assertEqual(status, HTTPStatus.OK)
        assert writer_tools is not None
        names = {row["name"] for row in writer_tools["result"]["tools"]}
        self.assertIn("create_entity", names)
        self.assertNotIn("get_metrics", names)

        status, writer_metrics = process_jsonrpc_message(
            app,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_metrics", "arguments": {}},
            },
            headers={"Authorization": "Bearer writer-token"},
        )
        self.assertEqual(status, HTTPStatus.OK)
        assert writer_metrics is not None
        self.assertTrue(writer_metrics["result"]["isError"])
        self.assertIn("AUTHZ_DENIED", writer_metrics["result"]["content"][0]["text"])

        status, maint_metrics = process_jsonrpc_message(
            app,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "get_metrics", "arguments": {}},
            },
            headers={"Authorization": "Bearer maint-token"},
        )
        self.assertEqual(status, HTTPStatus.OK)
        assert maint_metrics is not None
        self.assertFalse(maint_metrics["result"]["isError"])
        self.assertIn("total_calls", maint_metrics["result"]["structuredContent"])

        self.assertEqual(metrics_access_status(app, {}), HTTPStatus.UNAUTHORIZED)
        self.assertEqual(
            metrics_access_status(app, {"Authorization": "Bearer writer-token"}),
            HTTPStatus.FORBIDDEN,
        )
        self.assertEqual(
            metrics_access_status(app, {"Authorization": "Bearer maint-token"}),
            HTTPStatus.OK,
        )


if __name__ == "__main__":
    unittest.main()
