from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from archivist_mcp.db import connect
from archivist_mcp.errors import (
    ConflictError,
    ConstraintError,
    InvalidLifecycleTransitionError,
)
from archivist_mcp.migrations.runner import run_migrations
from archivist_mcp.storage.repository import GraphRepository


class GraphRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "archivist.db"
        self.conn = connect(str(self.db_path))
        run_migrations(self.conn, Path("archivist_mcp/migrations/sql"))
        self.repo = GraphRepository(self.conn)
        self.repo.create_project("proj-1", "Project One")
        self.repo.create_user("user-1", "Test User")

    def tearDown(self) -> None:
        self.conn.close()
        self.tmpdir.cleanup()

    def _node(self, node_id: str = "node-1"):
        return self.repo.create_node(
            node_id=node_id,
            project_id="proj-1",
            node_type="Decision",
            title="Node",
            content="Body",
            actor_id="user-1",
        )

    def test_valid_lifecycle_transitions(self) -> None:
        node = self._node()
        node = self.repo.update_node(
            node_id=node.node_id,
            expected_version=node.version,
            actor_id="user-1",
            state="deprecated",
        )
        self.assertEqual(node.state, "deprecated")

        node = self.repo.update_node(
            node_id=node.node_id,
            expected_version=node.version,
            actor_id="user-1",
            state="superseded",
        )
        self.assertEqual(node.state, "superseded")

    def test_invalid_lifecycle_transition_rejected(self) -> None:
        node = self._node()
        with self.assertRaises(InvalidLifecycleTransitionError):
            self.repo.update_node(
                node_id=node.node_id,
                expected_version=node.version,
                actor_id="user-1",
                state="superseded",
            )

    def test_version_conflict_reports_expected_and_actual(self) -> None:
        node = self._node()
        updated = self.repo.update_node(
            node_id=node.node_id,
            expected_version=1,
            actor_id="user-1",
            title="Updated",
        )
        self.assertEqual(updated.version, 2)

        with self.assertRaises(ConflictError) as ctx:
            self.repo.update_node(
                node_id=node.node_id,
                expected_version=1,
                actor_id="user-1",
                title="Stale",
            )

        self.assertIn("expected 1, actual 2", str(ctx.exception))

    def test_foreign_key_constraint_on_edge_insert(self) -> None:
        self._node(node_id="node-a")
        with self.assertRaises(ConstraintError):
            self.repo.create_edge(
                edge_id="edge-1",
                project_id="proj-1",
                edge_type="DEPENDS_ON",
                from_node_id="node-a",
                to_node_id="missing-node",
                actor_id="user-1",
            )

    def test_unique_active_edge_tuple_constraint(self) -> None:
        self._node(node_id="node-a")
        self._node(node_id="node-b")
        self.repo.create_edge(
            edge_id="edge-1",
            project_id="proj-1",
            edge_type="DEPENDS_ON",
            from_node_id="node-a",
            to_node_id="node-b",
            actor_id="user-1",
            state="active",
        )

        with self.assertRaises(ConstraintError):
            self.repo.create_edge(
                edge_id="edge-2",
                project_id="proj-1",
                edge_type="DEPENDS_ON",
                from_node_id="node-a",
                to_node_id="node-b",
                actor_id="user-1",
                state="active",
            )

    def test_audit_events_recorded_for_writes(self) -> None:
        node = self._node()
        self.repo.update_node(
            node_id=node.node_id,
            expected_version=node.version,
            actor_id="user-1",
            title="After",
        )

        rows = self.conn.execute(
            "SELECT action FROM audit_events ORDER BY event_id"
        ).fetchall()
        self.assertEqual([r["action"] for r in rows], ["node.create", "node.update"])

    def test_edge_version_conflict_reports_expected_and_actual(self) -> None:
        self._node(node_id="node-a")
        self._node(node_id="node-b")
        edge = self.repo.create_edge(
            edge_id="edge-1",
            project_id="proj-1",
            edge_type="DEPENDS_ON",
            from_node_id="node-a",
            to_node_id="node-b",
            actor_id="user-1",
        )
        self.assertEqual(edge.version, 1)
        self.repo.update_edge(
            edge_id="edge-1",
            expected_version=1,
            actor_id="user-1",
            state="deprecated",
        )

        with self.assertRaises(ConflictError) as ctx:
            self.repo.update_edge(
                edge_id="edge-1",
                expected_version=1,
                actor_id="user-1",
                state="deprecated",
            )

        self.assertIn("expected 1, actual 2", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
