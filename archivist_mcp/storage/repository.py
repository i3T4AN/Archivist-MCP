"""Data-access layer for graph storage."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from archivist_mcp.errors import (
    ConflictError,
    ConstraintError,
    InvalidLifecycleTransitionError,
    NotFoundError,
)

_ALLOWED_TRANSITIONS = {
    "active": {"active", "deprecated"},
    "deprecated": {"deprecated", "superseded", "invalidated"},
    "superseded": {"superseded"},
    "invalidated": {"invalidated"},
    "archived": {"archived"},
}

_EDGE_ALLOWED_TRANSITIONS = {
    "active": {"active", "deprecated"},
    "deprecated": {"deprecated"},
}


@dataclass
class NodeRecord:
    node_id: str
    project_id: str
    node_type: str
    title: str
    content: str
    state: str
    version: int


@dataclass
class EdgeRecord:
    edge_id: str
    project_id: str
    edge_type: str
    from_node_id: str
    to_node_id: str
    weight: float
    state: str
    version: int


class GraphRepository:
    """CRUD operations for nodes and edges with lifecycle and auditing guards."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create_project(self, project_id: str, name: str) -> None:
        self._execute(
            "INSERT INTO projects(project_id, name) VALUES (?, ?)",
            (project_id, name),
        )

    def create_user(self, user_id: str, display_name: str) -> None:
        self._execute(
            "INSERT INTO users(user_id, display_name) VALUES (?, ?)",
            (user_id, display_name),
        )

    def create_node(
        self,
        *,
        project_id: str,
        node_type: str,
        title: str,
        content: str,
        actor_id: str | None,
        state: str = "active",
        node_id: str | None = None,
    ) -> NodeRecord:
        node_id = node_id or str(uuid.uuid4())
        with self.conn:
            self._execute(
                """
                INSERT INTO nodes(node_id, project_id, type, title, content, state, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (node_id, project_id, node_type, title, content, state, actor_id),
            )
            self._insert_audit(
                project_id=project_id,
                actor_id=actor_id,
                action="node.create",
                target_id=node_id,
                details={"type": node_type, "state": state},
            )
        return self.get_node(node_id)

    def get_node(self, node_id: str) -> NodeRecord:
        row = self.conn.execute(
            "SELECT node_id, project_id, type, title, content, state, version FROM nodes WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"node not found: {node_id}")
        return NodeRecord(
            node_id=row["node_id"],
            project_id=row["project_id"],
            node_type=row["type"],
            title=row["title"],
            content=row["content"],
            state=row["state"],
            version=row["version"],
        )

    def update_node(
        self,
        *,
        node_id: str,
        expected_version: int,
        actor_id: str | None,
        title: str | None = None,
        content: str | None = None,
        state: str | None = None,
    ) -> NodeRecord:
        current = self.get_node(node_id)

        next_state = state if state is not None else current.state
        self._validate_transition(current.state, next_state)

        next_title = title if title is not None else current.title
        next_content = content if content is not None else current.content

        with self.conn:
            cur = self.conn.execute(
                """
                UPDATE nodes
                SET title = ?,
                    content = ?,
                    state = ?,
                    version = version + 1,
                    updated_at = (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                WHERE node_id = ? AND version = ?
                """,
                (next_title, next_content, next_state, node_id, expected_version),
            )
            if cur.rowcount == 0:
                actual = self.get_node(node_id).version
                self._insert_audit(
                    project_id=current.project_id,
                    actor_id=actor_id,
                    action="node.update.conflict",
                    target_id=node_id,
                    details={"expected_version": expected_version, "actual_version": actual},
                )
                raise ConflictError(
                    f"Version conflict for node {node_id}: expected {expected_version}, actual {actual}"
                )
            self._insert_audit(
                project_id=current.project_id,
                actor_id=actor_id,
                action="node.update",
                target_id=node_id,
                details={
                    "expected_version": expected_version,
                    "new_state": next_state,
                    "title_changed": title is not None,
                    "content_changed": content is not None,
                },
            )

        return self.get_node(node_id)

    def create_edge(
        self,
        *,
        project_id: str,
        edge_type: str,
        from_node_id: str,
        to_node_id: str,
        actor_id: str | None,
        weight: float = 1.0,
        state: str = "active",
        edge_id: str | None = None,
    ) -> EdgeRecord:
        edge_id = edge_id or str(uuid.uuid4())
        with self.conn:
            self._execute(
                """
                INSERT INTO edges(edge_id, project_id, type, from_node_id, to_node_id, weight, state)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (edge_id, project_id, edge_type, from_node_id, to_node_id, weight, state),
            )
            self._insert_audit(
                project_id=project_id,
                actor_id=actor_id,
                action="edge.create",
                target_id=edge_id,
                details={
                    "type": edge_type,
                    "from_node_id": from_node_id,
                    "to_node_id": to_node_id,
                    "weight": weight,
                    "state": state,
                },
            )
        return self.get_edge(edge_id)

    def upsert_node_property(self, node_id: str, key: str, value: Any) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO node_properties(node_id, key, value_json, updated_at)
                VALUES (?, ?, ?, (strftime('%Y-%m-%dT%H:%M:%fZ','now')))
                ON CONFLICT(node_id, key)
                DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (node_id, key, json.dumps(value, sort_keys=True)),
            )

    def get_edge(self, edge_id: str) -> EdgeRecord:
        row = self.conn.execute(
            """
            SELECT edge_id, project_id, type, from_node_id, to_node_id, weight, state, version
            FROM edges
            WHERE edge_id = ?
            """,
            (edge_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"edge not found: {edge_id}")
        return EdgeRecord(
            edge_id=row["edge_id"],
            project_id=row["project_id"],
            edge_type=row["type"],
            from_node_id=row["from_node_id"],
            to_node_id=row["to_node_id"],
            weight=row["weight"],
            state=row["state"],
            version=row["version"],
        )

    def update_edge(
        self,
        *,
        edge_id: str,
        expected_version: int,
        actor_id: str | None,
        weight: float | None = None,
        state: str | None = None,
    ) -> EdgeRecord:
        current = self.get_edge(edge_id)
        next_weight = weight if weight is not None else current.weight
        next_state = state if state is not None else current.state
        self._validate_edge_transition(current.state, next_state)

        with self.conn:
            cur = self.conn.execute(
                """
                UPDATE edges
                SET weight = ?,
                    state = ?,
                    version = version + 1,
                    updated_at = (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                WHERE edge_id = ? AND version = ?
                """,
                (next_weight, next_state, edge_id, expected_version),
            )
            if cur.rowcount == 0:
                actual = self.get_edge(edge_id).version
                self._insert_audit(
                    project_id=current.project_id,
                    actor_id=actor_id,
                    action="edge.update.conflict",
                    target_id=edge_id,
                    details={"expected_version": expected_version, "actual_version": actual},
                )
                raise ConflictError(
                    f"Version conflict for edge {edge_id}: expected {expected_version}, actual {actual}"
                )
            self._insert_audit(
                project_id=current.project_id,
                actor_id=actor_id,
                action="edge.update",
                target_id=edge_id,
                details={
                    "expected_version": expected_version,
                    "new_state": next_state,
                    "weight": next_weight,
                },
            )
        return self.get_edge(edge_id)

    def _validate_transition(self, old_state: str, new_state: str) -> None:
        allowed = _ALLOWED_TRANSITIONS.get(old_state, {old_state})
        if new_state not in allowed:
            raise InvalidLifecycleTransitionError(
                f"Invalid node lifecycle transition: {old_state} -> {new_state}"
            )

    def _validate_edge_transition(self, old_state: str, new_state: str) -> None:
        allowed = _EDGE_ALLOWED_TRANSITIONS.get(old_state, {old_state})
        if new_state not in allowed:
            raise InvalidLifecycleTransitionError(
                f"Invalid edge lifecycle transition: {old_state} -> {new_state}"
            )

    def _execute(self, sql: str, params: tuple[Any, ...]) -> sqlite3.Cursor:
        try:
            return self.conn.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            raise ConstraintError(str(exc)) from exc

    def _insert_audit(
        self,
        *,
        project_id: str,
        actor_id: str | None,
        action: str,
        target_id: str,
        details: dict[str, Any],
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO audit_events(project_id, actor_id, action, target_id, details_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (project_id, actor_id, action, target_id, json.dumps(details, sort_keys=True)),
        )

    def record_audit_event(
        self,
        *,
        project_id: str,
        actor_id: str | None,
        action: str,
        target_id: str,
        details: dict[str, Any],
    ) -> None:
        with self.conn:
            self._insert_audit(
                project_id=project_id,
                actor_id=actor_id,
                action=action,
                target_id=target_id,
                details=details,
            )
