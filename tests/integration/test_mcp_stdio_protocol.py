from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from archivist_mcp.db import connect
from archivist_mcp.migrations.runner import run_migrations

ROOT = Path(__file__).resolve().parents[2]


class McpStdioClient:
    def __init__(self, db_path: Path):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "archivist_mcp.mcp_stdio_server", "--db", str(db_path)],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            env=env,
        )

    def close(self) -> None:
        if self.proc.poll() is None:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=5)
        if self.proc.stdout:
            self.proc.stdout.close()
        if self.proc.stderr:
            self.proc.stderr.close()

    def request(self, message_id: int, method: str, params: dict | None = None) -> dict:
        self._send({"jsonrpc": "2.0", "id": message_id, "method": method, "params": params or {}})
        return self._read()

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _send(self, payload: dict) -> None:
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        frame = f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii") + raw
        assert self.proc.stdin is not None
        self.proc.stdin.write(frame)
        self.proc.stdin.flush()

    def _read(self) -> dict:
        assert self.proc.stdout is not None
        headers: dict[str, str] = {}
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP server closed stdout unexpectedly")
            if line in (b"\r\n", b"\n"):
                break
            decoded = line.decode("utf-8", errors="replace")
            if ":" not in decoded:
                continue
            name, value = decoded.split(":", 1)
            headers[name.strip().lower()] = value.strip()

        content_length = int(headers.get("content-length", "0"))
        if content_length <= 0:
            raise RuntimeError(f"Invalid content-length header: {headers!r}")
        body = self.proc.stdout.read(content_length)
        return json.loads(body.decode("utf-8"))


class McpStdioProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "mcp-protocol.db"
        conn = connect(str(self.db_path))
        run_migrations(conn, ROOT / "archivist_mcp/migrations/sql")
        conn.execute("INSERT INTO projects(project_id, name) VALUES ('proj-1', 'Project One')")
        conn.execute("INSERT INTO users(user_id, display_name) VALUES ('user-1', 'User One')")
        conn.commit()
        conn.close()
        self.client = McpStdioClient(self.db_path)

    def tearDown(self) -> None:
        self.client.close()
        self.tempdir.cleanup()

    def test_initialize_list_and_call_tools(self) -> None:
        init = self.client.request(
            1,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
        )
        self.assertEqual(init["jsonrpc"], "2.0")
        self.assertIn("result", init)
        self.assertIn("serverInfo", init["result"])
        self.assertIn("tools", init["result"]["capabilities"])

                                                                     
        self.client.notify("notifications/initialized", {})

        tool_list = self.client.request(2, "tools/list", {})
        self.assertIn("result", tool_list)
        names = {t["name"] for t in tool_list["result"]["tools"]}
        self.assertIn("health", names)
        self.assertIn("version", names)
        self.assertIn("get_capabilities", names)
        self.assertIn("get_metrics", names)
        self.assertIn("create_entity", names)
        self.assertIn("search_graph", names)

        version = self.client.request(21, "tools/call", {"name": "version", "arguments": {}})
        self.assertFalse(version["result"]["isError"])
        self.assertIn("server_version", version["result"]["structuredContent"])

        created = self.client.request(
            3,
            "tools/call",
            {
                "name": "create_entity",
                "arguments": {
                    "project_id": "proj-1",
                    "type": "Entity",
                    "title": "Parser",
                    "content": "Parses code",
                    "user_id": "user-1",
                },
            },
        )
        self.assertFalse(created["result"]["isError"])
        structured = created["result"]["structuredContent"]
        self.assertEqual(structured["node"]["title"], "Parser")

        search = self.client.request(
            4,
            "tools/call",
            {"name": "search_graph", "arguments": {"project_id": "proj-1", "query": "parser"}},
        )
        self.assertFalse(search["result"]["isError"])
        self.assertIn("results", search["result"]["structuredContent"])

        metrics = self.client.request(22, "tools/call", {"name": "get_metrics", "arguments": {}})
        self.assertFalse(metrics["result"]["isError"])
        self.assertIn("total_calls", metrics["result"]["structuredContent"])

    def test_unknown_tool_returns_error_result(self) -> None:
        self.client.request(
            1,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
        )
        self.client.notify("notifications/initialized", {})

        out = self.client.request(
            2,
            "tools/call",
            {"name": "does_not_exist", "arguments": {"project_id": "proj-1"}},
        )
        self.assertTrue(out["result"]["isError"])
        text = out["result"]["content"][0]["text"]
        self.assertIn("VALIDATION_ERROR", text)


if __name__ == "__main__":
    unittest.main()
