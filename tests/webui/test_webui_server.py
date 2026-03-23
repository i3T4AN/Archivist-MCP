from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from archivist_mcp.db import connect
from archivist_mcp.migrations.runner import run_migrations
from archivist_mcp.retrieval.embeddings import EmbeddingConfig, EmbeddingWorker
from archivist_mcp.retrieval.hybrid import HybridRetrievalEngine, RetrievalWeights
from archivist_mcp.team.auth import AuthContext
from archivist_mcp.tooling.server import ToolServer
from archivist_mcp.webui_server import WebUiApp


class WebUiServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "webui.db"
        self.conn = connect(str(self.db))
        run_migrations(self.conn, Path("archivist_mcp/migrations/sql"))
        self.conn.execute("INSERT INTO projects(project_id, name) VALUES ('p1','Project One')")
        self.conn.execute("INSERT INTO projects(project_id, name) VALUES ('p2','Project Two')")
        self.conn.execute("INSERT INTO users(user_id, display_name) VALUES ('u1','User One')")
        self.conn.execute(
            "INSERT INTO nodes(node_id, project_id, type, title, content, state, created_by) VALUES ('d1','p1','Decision','Use WAL','for durability','active','u1')"
        )
        self.conn.execute(
            "INSERT INTO nodes(node_id, project_id, type, title, content, state, created_by) VALUES ('i1','p1','Incident','Crash','panic in parser','deprecated','u1')"
        )
        self.conn.execute(
            "INSERT INTO nodes(node_id, project_id, type, title, content, state, created_by) VALUES ('d2','p2','Decision','Other','private','active','u1')"
        )
        self.conn.execute(
            "INSERT INTO edges(edge_id, project_id, type, from_node_id, to_node_id, weight, state) VALUES ('e1','p1','RESOLVED_BY','i1','d1',1.0,'active')"
        )
        self.conn.execute(
            "INSERT INTO audit_events(project_id, actor_id, action, target_id, details_json) VALUES (?, ?, ?, ?, ?)",
            ("p1", "u1", "tool.conflict", "update_entity", json.dumps({"message": "vconf"})),
        )
        self.conn.commit()

        embed = EmbeddingWorker(self.conn, EmbeddingConfig(enabled=True, provider="hash-local", dimensions=64))
        embed.rebuild_node_embeddings("p1")
        self.server = ToolServer(
            self.conn,
            require_user_id=False,
            embedding_worker=embed,
            retrieval_engine=HybridRetrievalEngine(self.conn, embed, RetrievalWeights()),
        )

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_search_graph_and_explain_data(self) -> None:
        app = WebUiApp(self.server, self.conn, tokens={}, team_mode=False)
        out = app.api_search("p1", "durability", 8, None)
        self.assertIn("data", out)
        self.assertGreaterEqual(len(out["data"]["results"]), 1)
        self.assertIn("provenance", out["data"]["results"][0])
        self.assertIn("confidence", out["data"]["results"][0])

    def test_read_views_have_expected_rows(self) -> None:
        app = WebUiApp(self.server, self.conn, tokens={}, team_mode=False)
        dec = app.api_decisions("p1")
        inc = app.api_incidents("p1")
        graph = app.api_graph("p1", None)
        conflicts = app.api_conflicts("p1")

        self.assertEqual(dec["decisions"][0]["node_id"], "d1")
        self.assertEqual(inc["incidents"][0]["resolved_by_id"], "d1")
        self.assertGreaterEqual(len(graph["nodes"]), 2)
        self.assertGreaterEqual(len(graph["edges"]), 1)
        self.assertEqual(conflicts["conflicts"][0]["action"], "tool.conflict")

    def test_tenant_isolation_scope_checks(self) -> None:
        reader_p1 = AuthContext(user_id="u1", role="reader", project_ids=("p1",))
        app = WebUiApp(
            self.server,
            self.conn,
            tokens={"tok": reader_p1},
            team_mode=True,
        )
        self.assertEqual(app.scope_error("p2", reader_p1), "project out of scope")
        self.assertEqual(app.scope_error(None, reader_p1), "project_id required")
        self.assertEqual(app.scope_error("p1", None), "unauthorized")
        self.assertIsNone(app.scope_error("p1", reader_p1))

    def test_static_assets_present(self) -> None:
        app = WebUiApp(self.server, self.conn, tokens={}, team_mode=False)
        html = (app.static_dir / "index.html").read_text(encoding="utf-8")
        js = (app.static_dir / "app.js").read_text(encoding="utf-8")
        css = (app.static_dir / "styles.css").read_text(encoding="utf-8")
        self.assertIn("Memory Control Room", html)
        self.assertIn("#/conflicts", html)
        self.assertIn("#/controls", html)
        self.assertIn("Explain-Why Panel", js)
        self.assertIn("/api/rules", js)
        self.assertIn("@media (max-width: 900px)", css)

    def test_writer_can_create_rule_and_audit_written(self) -> None:
        writer = AuthContext(user_id="u1", role="writer", project_ids=("p1",))
        app = WebUiApp(self.server, self.conn, tokens={"tok": writer}, team_mode=True)
        status, out = app.api_rule_write(
            {
                "project_id": "p1",
                "action": "create",
                "title": "Rule A",
                "content": "Do X",
                "severity": "warning",
                "enforcement": "advisory",
            },
            writer,
        )
        self.assertEqual(status, 200)
        self.assertIn("data", out)
        row = self.conn.execute(
            "SELECT action FROM audit_events WHERE project_id='p1' ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["action"], "webui.rule.create")

    def test_reader_cannot_write_rule(self) -> None:
        reader = AuthContext(user_id="u1", role="reader", project_ids=("p1",))
        app = WebUiApp(self.server, self.conn, tokens={"tok": reader}, team_mode=True)
        status, out = app.api_rule_write(
            {
                "project_id": "p1",
                "action": "create",
                "title": "Rule B",
                "content": "Do Y",
                "severity": "warning",
                "enforcement": "advisory",
            },
            reader,
        )
        self.assertEqual(status, 403)
        self.assertEqual(out["error"]["code"], "AUTHZ_DENIED")

    def test_conflict_is_surfaced_for_rule_update(self) -> None:
        writer = AuthContext(user_id="u1", role="writer", project_ids=("p1",))
        app = WebUiApp(self.server, self.conn, tokens={"tok": writer}, team_mode=True)
        create_status, create_out = app.api_rule_write(
            {
                "project_id": "p1",
                "action": "create",
                "title": "Rule C",
                "content": "Body",
                "severity": "warning",
                "enforcement": "advisory",
            },
            writer,
        )
        self.assertEqual(create_status, 200)
        node_id = create_out["data"]["node"]["node_id"]
        self.server.handle_tool(
            "update_entity",
            {"project_id": "p1", "node_id": node_id, "expected_version": 1, "content": "newer"},
            auth_context=writer,
        )
        status, out = app.api_rule_write(
            {
                "project_id": "p1",
                "action": "update",
                "node_id": node_id,
                "expected_version": 1,
                "content": "stale write",
            },
            writer,
        )
        self.assertEqual(status, 409)
        self.assertEqual(out["error"]["code"], "CONFLICT_ERROR")

    def test_promote_invalidate_and_conflict_resolve_actions(self) -> None:
        writer = AuthContext(user_id="u1", role="writer", project_ids=("p1",))
        app = WebUiApp(self.server, self.conn, tokens={"tok": writer}, team_mode=True)
        create_status, create_out = app.api_rule_write(
            {
                "project_id": "p1",
                "action": "create",
                "title": "Rule D",
                "content": "Body",
                "severity": "error",
                "enforcement": "gate_on_write",
            },
            writer,
        )
        self.assertEqual(create_status, 200)
        node_id = create_out["data"]["node"]["node_id"]
        self.server.repo.upsert_node_property(node_id, "scope", "branch")

        promote_status, promote_out = app.api_promote_scope(
            {
                "project_id": "p1",
                "node_id": node_id,
                "expected_version": 1,
                "resolution_note": "merged branch",
                "confirm": True,
            },
            writer,
        )
        self.assertEqual(promote_status, 200)
        self.assertEqual(promote_out["data"]["scope"], "project")

        invalidate_status, invalidate_out = app.api_invalidate_memory(
            {
                "project_id": "p1",
                "node_id": node_id,
                "expected_version": 2,
                "reason": "stale memory",
                "confirm": True,
            },
            writer,
        )
        self.assertEqual(invalidate_status, 200)
        self.assertEqual(invalidate_out["data"]["node"]["state"], "invalidated")

        conflict_event_id = self.conn.execute(
            "SELECT event_id FROM audit_events WHERE project_id='p1' AND action='tool.conflict' ORDER BY event_id DESC LIMIT 1"
        ).fetchone()["event_id"]
        resolve_status, resolve_out = app.api_conflict_resolve(
            {
                "project_id": "p1",
                "conflict_event_id": conflict_event_id,
                "resolution_note": "accepted latest",
                "confirm": True,
            },
            writer,
        )
        self.assertEqual(resolve_status, 200)
        self.assertTrue(resolve_out["data"]["resolved"])


if __name__ == "__main__":
    unittest.main()
