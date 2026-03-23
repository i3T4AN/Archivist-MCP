from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import shutil

from archivist_mcp.db import connect
from archivist_mcp.migrations.runner import run_migrations

ROOT = Path(__file__).resolve().parents[2]


class StdioClient:
    def __init__(
        self,
        db_path: Path,
        require_user_id: bool = False,
        env_overrides: dict[str, str] | None = None,
    ):
        cmd = [sys.executable, "-m", "archivist_mcp.stdio_server", "--db", str(db_path)]
        if require_user_id:
            cmd.append("--require-user-id")

        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        if env_overrides:
            env.update(env_overrides)
        self.proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

    def call(self, tool: str, args: dict, req_id: int = 1) -> dict:
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        self.proc.stdin.write(json.dumps({"id": req_id, "tool": tool, "args": args}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        return json.loads(line)["result"]

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


class StdioToolIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "integration.db"
        conn = connect(str(self.db_path))
        run_migrations(conn, ROOT / "archivist_mcp/migrations/sql")
        conn.execute("INSERT INTO projects(project_id, name) VALUES ('proj-1', 'Project One')")
        conn.execute("INSERT INTO users(user_id, display_name) VALUES ('user-1', 'User One')")
        conn.execute(
            """
            INSERT INTO nodes(node_id, project_id, type, title, content, state, created_by)
            VALUES ('node-1', 'proj-1', 'Entity', 'Parser', 'parses code', 'active', 'user-1')
            """
        )
        conn.execute(
            """
            INSERT INTO nodes(node_id, project_id, type, title, content, state, created_by)
            VALUES ('incident-1', 'proj-1', 'Incident', 'Crash', 'app crashes', 'active', 'user-1')
            """
        )
        conn.commit()
        conn.close()
        self.client = StdioClient(self.db_path)
        self.fixture_repo = Path(self.tempdir.name) / "fixture_repo"
        shutil.copytree(ROOT / "tests/indexing/fixtures/repo_a", self.fixture_repo)

    def tearDown(self) -> None:
        self.client.close()
        self.tempdir.cleanup()

    def _assert_ok_envelope(self, response: dict) -> None:
        self.assertIn("trace_id", response)
        self.assertEqual(response["version"], 1)
        self.assertIn("warnings", response)
        self.assertIn("data", response)

    def test_create_entity(self) -> None:
        response = self.client.call(
            "create_entity",
            {
                "project_id": "proj-1",
                "type": "Entity",
                "title": "Indexer",
                "content": "indexes files",
                "user_id": "user-1",
            },
        )
        self._assert_ok_envelope(response)
        self.assertEqual(response["data"]["node"]["title"], "Indexer")

    def test_health_version_and_capabilities_tools(self) -> None:
        health = self.client.call("health", {})
        self._assert_ok_envelope(health)
        self.assertEqual(health["data"]["status"], "ok")

        version = self.client.call("version", {})
        self._assert_ok_envelope(version)
        self.assertIn("server_version", version["data"])
        self.assertEqual(version["data"]["envelope_version"], 1)

        caps = self.client.call("get_capabilities", {})
        self._assert_ok_envelope(caps)
        self.assertIn("tools", caps["data"])
        self.assertIn("health", caps["data"]["tools"])
        self.assertIn("search_graph", caps["data"]["tools"])

        metrics = self.client.call("get_metrics", {})
        self._assert_ok_envelope(metrics)
        self.assertIn("total_calls", metrics["data"])
        self.assertIn("by_tool", metrics["data"])

    def test_rate_limited_error_shape(self) -> None:
        self.client.close()
        self.client = StdioClient(
            self.db_path,
            env_overrides={
                "ARCHIVIST_RATE_LIMIT_PER_MINUTE": "1",
                "ARCHIVIST_STRUCTURED_LOGGING": "false",
            },
        )
        first = self.client.call("health", {})
        second = self.client.call("health", {})
        self._assert_ok_envelope(first)
        self.assertIn("error", second)
        self.assertEqual(second["error"]["code"], "RATE_LIMITED")

    def test_read_node(self) -> None:
        response = self.client.call(
            "read_node", {"project_id": "proj-1", "node_id": "node-1"}
        )
        self._assert_ok_envelope(response)
        self.assertEqual(response["data"]["node"]["node_id"], "node-1")

    def test_update_entity(self) -> None:
        response = self.client.call(
            "update_entity",
            {
                "project_id": "proj-1",
                "node_id": "node-1",
                "expected_version": 1,
                "title": "ParserV2",
                "user_id": "user-1",
            },
        )
        self._assert_ok_envelope(response)
        self.assertEqual(response["data"]["node"]["version"], 2)

    def test_create_edge(self) -> None:
        self.client.call(
            "create_entity",
            {
                "project_id": "proj-1",
                "node_id": "node-2",
                "type": "Entity",
                "title": "Analyzer",
                "content": "analyzes",
                "user_id": "user-1",
            },
        )
        response = self.client.call(
            "create_edge",
            {
                "project_id": "proj-1",
                "type": "DEPENDS_ON",
                "from_node_id": "node-1",
                "to_node_id": "node-2",
                "user_id": "user-1",
            },
        )
        self._assert_ok_envelope(response)
        self.assertEqual(response["data"]["edge"]["type"], "DEPENDS_ON")

    def test_search_graph(self) -> None:
        self.client.call(
            "rebuild_embeddings",
            {"project_id": "proj-1"},
        )
        response = self.client.call(
            "search_graph", {"project_id": "proj-1", "query": "parses"}
        )
        self._assert_ok_envelope(response)
        self.assertEqual(response["data"]["mode"], "hybrid")
        self.assertGreaterEqual(len(response["data"]["results"]), 1)
        self.assertIn("provenance", response["data"]["results"][0])
        self.assertIn("confidence", response["data"]["results"][0])

    def test_store_observation(self) -> None:
        response = self.client.call(
            "store_observation",
            {
                "project_id": "proj-1",
                "text": "compiler warning seen",
                "source": "test",
                "user_id": "user-1",
            },
        )
        self._assert_ok_envelope(response)
        self.assertTrue(response["data"]["observation_id"])

    def test_archive_decision(self) -> None:
        response = self.client.call(
            "archive_decision",
            {
                "project_id": "proj-1",
                "title": "Use sqlite",
                "content": "fits local-first",
                "problem_statement": "Need durable store",
                "decision": "Use sqlite",
                "alternatives_considered": ["postgres"],
                "tradeoffs": ["less scale"],
                "status": "accepted",
                "user_id": "user-1",
            },
        )
        self._assert_ok_envelope(response)
        self.assertEqual(response["data"]["node"]["type"], "Decision")

    def test_get_project_summary(self) -> None:
        response = self.client.call("get_project_summary", {"project_id": "proj-1"})
        self._assert_ok_envelope(response)
        self.assertEqual(response["data"]["project_id"], "proj-1")

    def test_list_recent_incidents(self) -> None:
        response = self.client.call(
            "list_recent_incidents", {"project_id": "proj-1", "limit": 5}
        )
        self._assert_ok_envelope(response)
        self.assertGreaterEqual(len(response["data"]["incidents"]), 1)

    def test_deprecate_node(self) -> None:
        response = self.client.call(
            "deprecate_node",
            {
                "project_id": "proj-1",
                "node_id": "node-1",
                "expected_version": 1,
                "user_id": "user-1",
            },
        )
        self._assert_ok_envelope(response)
        self.assertEqual(response["data"]["node"]["state"], "deprecated")

    def test_invalid_payload_returns_validation_error_shape(self) -> None:
        response = self.client.call("create_entity", {"project_id": "proj-1"})
        self.assertIn("error", response)
        self.assertEqual(response["error"]["code"], "VALIDATION_ERROR")
        self.assertIn("message", response["error"])
        self.assertEqual(response["error"]["details"], {})

    def test_idempotency_key_prevents_duplicate_write(self) -> None:
        payload = {
            "project_id": "proj-1",
            "type": "Entity",
            "title": "Idempotent",
            "content": "once",
            "user_id": "user-1",
            "idempotency_key": "dup-1",
        }
        first = self.client.call("create_entity", payload)
        second = self.client.call("create_entity", payload)

        self._assert_ok_envelope(first)
        self._assert_ok_envelope(second)
        self.assertIn("IDEMPOTENT_REPLAY", second["warnings"])

        conn = sqlite3.connect(self.db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE project_id='proj-1' AND title='Idempotent'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_team_mode_requires_user_id(self) -> None:
        self.client.close()
        self.client = StdioClient(self.db_path, require_user_id=True)
        response = self.client.call(
            "create_entity",
            {
                "project_id": "proj-1",
                "type": "Entity",
                "title": "Needs user",
                "content": "x",
            },
        )
        self.assertIn("error", response)
        self.assertEqual(response["error"]["code"], "AUTHZ_DENIED")

    def test_write_triggers_core_memory_refresh(self) -> None:
        response = self.client.call(
            "create_entity",
            {
                "project_id": "proj-1",
                "type": "Entity",
                "title": "CoreRefresh",
                "content": "trigger",
                "user_id": "user-1",
            },
        )
        self._assert_ok_envelope(response)
        self.assertIn("core_memory_truncated", response["data"])
        self.assertTrue((self.db_path.parent / "core_memory.json").exists())
        self.assertTrue((self.db_path.parent / "core_memory.md").exists())

    def test_compact_core_memory_manual_refresh(self) -> None:
        response = self.client.call("compact_core_memory", {"project_id": "proj-1"})
        self._assert_ok_envelope(response)
        self.assertEqual(response["data"]["core_memory"]["project_id"], "proj-1")

    def test_extract_symbols_tool(self) -> None:
        response = self.client.call(
            "extract_symbols",
            {
                "project_id": "proj-1",
                "root_path": str(self.fixture_repo),
                "incremental": True,
            },
        )
        self._assert_ok_envelope(response)
        report = response["data"]["report"]
        self.assertGreater(report["scanned_files"], 0)
        self.assertGreater(report["symbols_added_or_updated"], 0)

    def test_rebuild_index_and_embeddings_tool(self) -> None:
        response = self.client.call(
            "rebuild_index_and_embeddings",
            {
                "project_id": "proj-1",
                "root_path": str(self.fixture_repo),
            },
        )
        self._assert_ok_envelope(response)
        self.assertIn("index_report", response["data"])
        self.assertGreaterEqual(response["data"]["index_report"]["scanned_files"], 1)

    def test_disabled_embedding_fallback_warning(self) -> None:
        self.client.close()
        self.client = StdioClient(
            self.db_path,
            env_overrides={"ARCHIVIST_DISABLE_EMBEDDINGS": "true"},
        )
        response = self.client.call(
            "search_graph",
            {"project_id": "proj-1", "query": "parses"},
        )
        self._assert_ok_envelope(response)
        self.assertEqual(response["data"]["mode"], "fts_graph")
        self.assertIn("EMBEDDING_DISABLED", response["warnings"])


if __name__ == "__main__":
    unittest.main()
