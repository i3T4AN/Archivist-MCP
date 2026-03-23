from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from archivist_mcp.db import connect
from archivist_mcp.migrations.runner import run_migrations
from archivist_mcp.retrieval.embeddings import EmbeddingConfig, EmbeddingWorker
from archivist_mcp.retrieval.hybrid import HybridRetrievalEngine, RetrievalWeights
from archivist_mcp.team.auth import AuthContext
from archivist_mcp.tooling.server import ToolServer


class TeamModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "team.db"
        conn = connect(str(self.db))
        run_migrations(conn, Path("archivist_mcp/migrations/sql"))
        conn.execute("INSERT INTO projects(project_id, name) VALUES ('p1','Project1')")
        conn.execute("INSERT INTO projects(project_id, name) VALUES ('p2','Project2')")
        conn.execute("INSERT INTO users(user_id, display_name) VALUES ('u1','User1')")
        conn.execute("INSERT INTO users(user_id, display_name) VALUES ('u2','User2')")
        conn.execute(
            "INSERT INTO nodes(node_id, project_id, type, title, content, state, created_by) VALUES ('d1','p1','Decision','Dec','Body','active','u1')"
        )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _server(self):
        conn = connect(str(self.db))
        embed = EmbeddingWorker(conn, EmbeddingConfig(enabled=True, provider="hash-local", dimensions=64))
        engine = HybridRetrievalEngine(conn, embed, RetrievalWeights())
        return conn, ToolServer(conn, require_user_id=True, embedding_worker=embed, retrieval_engine=engine)

    def test_cross_project_isolation_denied(self) -> None:
        conn, server = self._server()
        try:
            ctx = AuthContext(user_id="u1", role="writer", project_ids=("p1",))
            resp = server.handle_tool(
                "create_entity",
                {"project_id": "p2", "type": "Entity", "title": "X", "content": "Y"},
                auth_context=ctx,
            )
            self.assertIn("error", resp)
            self.assertEqual(resp["error"]["code"], "AUTHZ_DENIED")
        finally:
            conn.close()

    def test_unauthorized_role_denied(self) -> None:
        conn, server = self._server()
        try:
            ctx = AuthContext(user_id="u1", role="reader", project_ids=("p1",))
            resp = server.handle_tool(
                "create_entity",
                {"project_id": "p1", "type": "Entity", "title": "X", "content": "Y"},
                auth_context=ctx,
            )
            self.assertIn("error", resp)
            self.assertEqual(resp["error"]["code"], "AUTHZ_DENIED")
        finally:
            conn.close()

    def test_concurrent_writers_conflict_with_context(self) -> None:
        c1, s1 = self._server()
        c2, s2 = self._server()
        try:
            ctx1 = AuthContext(user_id="u1", role="writer", project_ids=("p1",))
            ctx2 = AuthContext(user_id="u2", role="writer", project_ids=("p1",))
                                                                                                
            ok = s1.handle_tool(
                "update_entity",
                {
                    "project_id": "p1",
                    "node_id": "d1",
                    "expected_version": 1,
                    "content": "A",
                    "decision_rationale": "rationale-A",
                },
                auth_context=ctx1,
            )
            conflict = s2.handle_tool(
                "update_entity",
                {
                    "project_id": "p1",
                    "node_id": "d1",
                    "expected_version": 1,
                    "content": "B",
                    "decision_rationale": "rationale-B",
                },
                auth_context=ctx2,
            )
            self.assertIn("data", ok)
            self.assertIn("error", conflict)
            self.assertEqual(conflict["error"]["code"], "CONFLICT_ERROR")
            self.assertIn("base", conflict["error"]["details"])
            self.assertIn("contender", conflict["error"]["details"])
        finally:
            c1.close()
            c2.close()

    def test_authorization_matrix_roles(self) -> None:
        conn, server = self._server()
        try:
            reader = AuthContext(user_id="u1", role="reader", project_ids=("p1",))
            writer = AuthContext(user_id="u1", role="writer", project_ids=("p1",))
            maintainer = AuthContext(user_id="u1", role="maintainer", project_ids=("p1",))
            admin = AuthContext(user_id="u1", role="admin", project_ids=("p1",))

                                        
            health_ok = server.handle_tool("health", {}, auth_context=reader)
            ok = server.handle_tool("read_node", {"project_id": "p1", "node_id": "d1"}, auth_context=reader)
            denied = server.handle_tool(
                "create_entity",
                {"project_id": "p1", "type": "Entity", "title": "X", "content": "Y"},
                auth_context=reader,
            )
            self.assertIn("data", health_ok)
            self.assertIn("data", ok)
            self.assertEqual(denied["error"]["code"], "AUTHZ_DENIED")

                                                  
            ok_writer = server.handle_tool(
                "create_entity",
                {"project_id": "p1", "type": "Entity", "title": "W", "content": "Y"},
                auth_context=writer,
            )
            denied_writer = server.handle_tool("export_audit_log", {"project_id": "p1"}, auth_context=writer)
            denied_metrics = server.handle_tool("get_metrics", {}, auth_context=writer)
            self.assertIn("data", ok_writer)
            self.assertEqual(denied_writer["error"]["code"], "AUTHZ_DENIED")
            self.assertEqual(denied_metrics["error"]["code"], "AUTHZ_DENIED")

                                                  
            ok_maint = server.handle_tool("export_audit_log", {"project_id": "p1"}, auth_context=maintainer)
            ok_metrics = server.handle_tool("get_metrics", {}, auth_context=maintainer)
            self.assertIn("data", ok_maint)
            self.assertIn("data", ok_metrics)

                                                            
            admin_denied = server.handle_tool("unknown_tool", {"project_id": "p1"}, auth_context=admin)
            self.assertEqual(admin_denied["error"]["code"], "VALIDATION_ERROR")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
