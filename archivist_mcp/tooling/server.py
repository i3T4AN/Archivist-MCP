"""MCP-style tool router and handlers for v1 tools."""

from __future__ import annotations

import json
import hashlib
import logging
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from archivist_mcp.config import SecurityConfig
from archivist_mcp.errors import (
    ConflictError,
    ConflictWithContextError,
    ConstraintError,
    InvalidLifecycleTransitionError,
    NotFoundError,
)
from archivist_mcp.storage.repository import GraphRepository
from archivist_mcp.tooling.errors import ValidationError
from archivist_mcp.memory.materializer import CoreMemoryMaterializer
from archivist_mcp.indexing.indexer import SymbolIndexer
from archivist_mcp.retrieval.embeddings import EmbeddingWorker
from archivist_mcp.retrieval.hybrid import HybridRetrievalEngine
from archivist_mcp.security.input import normalize_text, sanitize_text
from archivist_mcp.security.redaction import redact_sensitive
from archivist_mcp.team.auth import AuthContext, can_call_tool, is_project_allowed
from archivist_mcp.observability.alerts import AlertConfig, AlertPipeline
from archivist_mcp.observability.logging import log_event
from archivist_mcp.observability.metrics import InMemoryMetrics
from archivist_mcp.observability.rate_limit import RateLimiter

EXPERIMENTAL_TOOLS = {"extract_symbols"}


class ToolServer:
    """Routes tool calls and returns deterministic envelope responses."""

    ENVELOPE_VERSION = 1
    SERVER_VERSION = "0.1.0-rc1"

    def list_tools(self) -> list[str]:
        return sorted(name for name in self._handlers if self._is_tool_enabled(name))

    def _is_tool_enabled(self, tool_name: str) -> bool:
        if tool_name in self.disabled_tools:
            return False
        if not self.enable_experimental_tools and tool_name in EXPERIMENTAL_TOOLS:
            return False
        return True

    def __init__(
        self,
        conn: sqlite3.Connection,
        require_user_id: bool = False,
        core_materializer: CoreMemoryMaterializer | None = None,
        embedding_worker: EmbeddingWorker | None = None,
        retrieval_engine: HybridRetrievalEngine | None = None,
        event_emitter: Callable[[dict[str, Any]], None] | None = None,
        security: SecurityConfig | None = None,
        metrics: InMemoryMetrics | None = None,
        rate_limiter: RateLimiter | None = None,
        alert_pipeline: AlertPipeline | None = None,
        logger: logging.Logger | None = None,
        enable_experimental_tools: bool = True,
        disabled_tools: set[str] | None = None,
    ):
        self.conn = conn
        self.repo = GraphRepository(conn)
        self.require_user_id = require_user_id
        self.core_materializer = core_materializer
        self.embedding_worker = embedding_worker
        self.retrieval_engine = retrieval_engine
        self.event_emitter = event_emitter
        self.security = security or SecurityConfig()
        self.metrics = metrics or InMemoryMetrics()
        self.rate_limiter = rate_limiter or RateLimiter(enabled=False, per_actor_per_minute=1_000_000)
        self.alert_pipeline = alert_pipeline or AlertPipeline(
            AlertConfig(enabled=False, min_calls=1, error_rate_threshold=1.0, cooldown_seconds=3600)
        )
        self.logger = logger
        self.enable_experimental_tools = enable_experimental_tools
        self.disabled_tools = disabled_tools or set()
        self._lock = threading.RLock()
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "health": self._health,
            "version": self._version,
            "get_capabilities": self._get_capabilities,
            "get_metrics": self._get_metrics,
            "create_entity": self._create_entity,
            "read_node": self._read_node,
            "update_entity": self._update_entity,
            "create_edge": self._create_edge,
            "search_graph": self._search_graph,
            "store_observation": self._store_observation,
            "archive_decision": self._archive_decision,
            "get_project_summary": self._get_project_summary,
            "list_recent_incidents": self._list_recent_incidents,
            "deprecate_node": self._deprecate_node,
            "compact_core_memory": self._compact_core_memory,
            "extract_symbols": self._extract_symbols,
            "rebuild_embeddings": self._rebuild_embeddings,
            "rebuild_index_and_embeddings": self._rebuild_index_and_embeddings,
            "export_audit_log": self._export_audit_log,
            "purge_observations": self._purge_observations,
            "resolve_conflict": self._resolve_conflict,
            "promote_branch_record": self._promote_branch_record,
            "invalidate_stale_memory": self._invalidate_stale_memory,
        }

    def handle_tool(
        self,
        tool_name: str,
        payload: dict[str, Any],
        trace_id: str | None = None,
        auth_context: AuthContext | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            trace = trace_id or str(uuid.uuid4())
            warnings: list[str] = []
            start = time.perf_counter()
            payload = dict(payload)
            response: dict[str, Any]
            error_code: str | None = None

            try:
                if tool_name not in self._handlers:
                    raise ValidationError(f"unknown tool: {tool_name}")
                if not self._is_tool_enabled(tool_name):
                    raise ValidationError(f"tool disabled: {tool_name}")
                payload, sanitized = self._validate_and_sanitize(tool_name, payload)
                self._authorize(tool_name, payload, auth_context)
                actor = self._rate_limit_actor(payload, auth_context)
                if not self.rate_limiter.allow(actor=actor, tool=tool_name):
                    error_code = "RATE_LIMITED"
                    response = self._error(
                        trace,
                        "RATE_LIMITED",
                        f"rate limit exceeded for actor {actor}",
                    )
                    return self._record_and_log(
                        tool_name=tool_name,
                        trace_id=trace,
                        start=start,
                        response=response,
                        error_code=error_code,
                    )
                result = self._handlers[tool_name](payload)
                if sanitized:
                    warnings.append("SANITIZED_INPUT")
                if result.get("_warnings"):
                    warnings.extend(result["_warnings"])
                    del result["_warnings"]
                if result.get("_idempotent_replay"):
                    warnings.append("IDEMPOTENT_REPLAY")
                    del result["_idempotent_replay"]
                response = {
                    "trace_id": trace,
                    "version": self.ENVELOPE_VERSION,
                    "warnings": warnings,
                    "data": result,
                }
                return self._record_and_log(
                    tool_name=tool_name,
                    trace_id=trace,
                    start=start,
                    response=response,
                    error_code=None,
                )
            except ValidationError as exc:
                error_code = "VALIDATION_ERROR"
                response = self._error(trace, "VALIDATION_ERROR", str(exc))
                return self._record_and_log(
                    tool_name=tool_name,
                    trace_id=trace,
                    start=start,
                    response=response,
                    error_code=error_code,
                )
            except (ConflictError, InvalidLifecycleTransitionError, ConstraintError) as exc:
                details = getattr(exc, "details", {})
                self._record_conflict_audit(payload, auth_context, tool_name, str(exc), details)
                self._emit_event(
                    {
                        "event": "conflict",
                        "tool": tool_name,
                        "project_id": payload.get("project_id"),
                        "actor_id": auth_context.user_id if auth_context else payload.get("user_id"),
                        "message": str(exc),
                        "details": details,
                    }
                )
                error_code = "CONFLICT_ERROR"
                response = self._error(trace, "CONFLICT_ERROR", str(exc), details)
                return self._record_and_log(
                    tool_name=tool_name,
                    trace_id=trace,
                    start=start,
                    response=response,
                    error_code=error_code,
                )
            except NotFoundError as exc:
                error_code = "NOT_FOUND"
                response = self._error(trace, "NOT_FOUND", str(exc))
                return self._record_and_log(
                    tool_name=tool_name,
                    trace_id=trace,
                    start=start,
                    response=response,
                    error_code=error_code,
                )
            except PermissionError as exc:
                error_code = "AUTHZ_DENIED"
                response = self._error(trace, "AUTHZ_DENIED", str(exc))
                return self._record_and_log(
                    tool_name=tool_name,
                    trace_id=trace,
                    start=start,
                    response=response,
                    error_code=error_code,
                )
            except Exception as exc:                                      
                error_code = "INTERNAL_STORAGE_ERROR"
                response = self._error(trace, "INTERNAL_STORAGE_ERROR", str(exc))
                return self._record_and_log(
                    tool_name=tool_name,
                    trace_id=trace,
                    start=start,
                    response=response,
                    error_code=error_code,
                )

    def _error(
        self, trace_id: str, code: str, message: str, details: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {
            "trace_id": trace_id,
            "version": self.ENVELOPE_VERSION,
            "warnings": [],
            "error": {"code": code, "message": message, "details": details or {}},
        }

    def _health(self, payload: dict[str, Any]) -> dict[str, Any]:
        _ = payload
        self.conn.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    def _version(self, payload: dict[str, Any]) -> dict[str, Any]:
        _ = payload
        return {"server_version": self.SERVER_VERSION, "envelope_version": self.ENVELOPE_VERSION}

    def _get_capabilities(self, payload: dict[str, Any]) -> dict[str, Any]:
        _ = payload
        return {
            "tools": self.list_tools(),
            "error_codes": [
                "VALIDATION_ERROR",
                "CONFLICT_ERROR",
                "NOT_FOUND",
                "AUTHZ_DENIED",
                "RATE_LIMITED",
                "INTERNAL_STORAGE_ERROR",
            ],
            "experimental_tools_enabled": self.enable_experimental_tools,
            "disabled_tools": sorted(self.disabled_tools),
            "envelope_version": self.ENVELOPE_VERSION,
            "server_version": self.SERVER_VERSION,
        }

    def _get_metrics(self, payload: dict[str, Any]) -> dict[str, Any]:
        _ = payload
        return self.metrics.snapshot()

    def _create_entity(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = self._require_str(payload, "project_id")
        node_type = self._require_str(payload, "type")
        title = self._require_str(payload, "title")
        content = self._require_str(payload, "content")
        actor_id = self._optional_str(payload, "user_id")
        self._enforce_user(actor_id)
        state = self._optional_str(payload, "state") or "active"

        def run() -> dict[str, Any]:
            node = self.repo.create_node(
                node_id=self._optional_str(payload, "node_id"),
                project_id=project_id,
                node_type=node_type,
                title=title,
                content=content,
                actor_id=actor_id,
                state=state,
            )
            data: dict[str, Any] = {"node": self._node_response(node)}
            self._embed_node(node.node_id, title, content)
            self._refresh_core_memory(project_id, data)
            return data

        return self._idempotent_write(
            project_id=project_id,
            tool_name="create_entity",
            idempotency_key=self._optional_str(payload, "idempotency_key"),
            writer=run,
        )

    def _read_node(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = self._require_str(payload, "project_id")
        node_id = self._require_str(payload, "node_id")
        node = self.repo.get_node(node_id)
        if node.project_id != project_id:
            raise NotFoundError(f"node not found: {node_id}")

        props = self.conn.execute(
            "SELECT key, value_json FROM node_properties WHERE node_id = ? ORDER BY key",
            (node_id,),
        ).fetchall()
        return {
            "node": self._node_response(node),
            "properties": [
                {"key": row["key"], "value_json": json.loads(row["value_json"])} for row in props
            ],
        }

    def _update_entity(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = self._require_str(payload, "project_id")
        node_id = self._require_str(payload, "node_id")
        expected_version = self._require_int(payload, "expected_version")
        actor_id = self._optional_str(payload, "user_id")
        self._enforce_user(actor_id)
        decision_rationale = self._optional_str(payload, "decision_rationale")
        metadata_union = payload.get("metadata_union") if isinstance(payload.get("metadata_union"), dict) else None

        def run() -> dict[str, Any]:
            current = self.repo.get_node(node_id)
            if current.project_id != project_id:
                raise NotFoundError(f"node not found: {node_id}")
            if decision_rationale is not None and current.node_type != "Decision":
                raise ValidationError("decision_rationale is only valid for Decision nodes")
            try:
                node = self.repo.update_node(
                    node_id=node_id,
                    expected_version=expected_version,
                    actor_id=actor_id,
                    title=self._optional_str(payload, "title"),
                    content=self._optional_str(payload, "content"),
                    state=self._optional_str(payload, "state"),
                )
            except ConflictError as exc:
                                                                                       
                if decision_rationale is not None and current.node_type == "Decision":
                    latest = self.repo.get_node(node_id)
                    raise ConflictWithContextError(
                        str(exc),
                        details={
                            "base": {"node_id": current.node_id, "version": expected_version},
                            "contender": {
                                "node_id": latest.node_id,
                                "version": latest.version,
                                "title": latest.title,
                                "content": latest.content,
                            },
                        },
                    ) from exc
                raise
            if decision_rationale is not None:
                self.repo.upsert_node_property(node.node_id, "decision_rationale", decision_rationale)
            if metadata_union:
                self._apply_metadata_union(node.node_id, metadata_union)
            data: dict[str, Any] = {"node": self._node_response(node)}
            self._embed_node(node.node_id, node.title, node.content)
            self._refresh_core_memory(project_id, data)
            return data

        return self._idempotent_write(
            project_id=project_id,
            tool_name="update_entity",
            idempotency_key=self._optional_str(payload, "idempotency_key"),
            writer=run,
        )

    def _create_edge(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = self._require_str(payload, "project_id")
        edge_type = self._require_str(payload, "type")
        from_node_id = self._require_str(payload, "from_node_id")
        to_node_id = self._require_str(payload, "to_node_id")
        actor_id = self._optional_str(payload, "user_id")
        self._enforce_user(actor_id)

        weight = self._optional_float(payload, "weight")
        state = self._optional_str(payload, "state") or "active"

        def run() -> dict[str, Any]:
            active_outgoing = self.conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM edges
                WHERE project_id = ? AND from_node_id = ? AND state = 'active'
                """,
                (project_id, from_node_id),
            ).fetchone()["count"]
            if active_outgoing >= self.security.edge_fanout_limit:
                raise ValidationError(
                    f"edge fan-out limit exceeded ({self.security.edge_fanout_limit}) for {from_node_id}"
                )
            edge = self.repo.create_edge(
                edge_id=self._optional_str(payload, "edge_id"),
                project_id=project_id,
                edge_type=edge_type,
                from_node_id=from_node_id,
                to_node_id=to_node_id,
                actor_id=actor_id,
                weight=1.0 if weight is None else weight,
                state=state,
            )
            data: dict[str, Any] = {"edge": self._edge_response(edge)}
            self._refresh_core_memory(project_id, data)
            return data

        return self._idempotent_write(
            project_id=project_id,
            tool_name="create_edge",
            idempotency_key=self._optional_str(payload, "idempotency_key"),
            writer=run,
        )

    def _search_graph(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = self._require_str(payload, "project_id")
        query = self._require_str(payload, "query")
        limit = self._optional_int(payload, "limit") or 8
        include_deprecated = bool(payload.get("include_deprecated", False))
        if self.retrieval_engine is None:
            raise ValidationError("retrieval engine not configured")
        out = self.retrieval_engine.search(
            project_id=project_id,
            query=query,
            limit=limit,
            include_deprecated=include_deprecated,
        )
        return {"results": out["results"], "mode": out["mode"], "_warnings": out.get("warnings", [])}

    def _store_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = self._require_str(payload, "project_id")
        text = self._require_str(payload, "text")
        actor_id = self._optional_str(payload, "user_id")
        self._enforce_user(actor_id)

        def run() -> dict[str, Any]:
            observation_id = self._optional_str(payload, "observation_id") or str(uuid.uuid4())
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO observations(observation_id, project_id, session_id, text, source, confidence)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation_id,
                        project_id,
                        self._optional_str(payload, "session_id"),
                        text,
                        self._optional_str(payload, "source"),
                        self._optional_float(payload, "confidence"),
                    ),
                )
                self.conn.execute(
                    """
                    INSERT INTO audit_events(project_id, actor_id, action, target_id, details_json)
                    VALUES (?, ?, 'observation.create', ?, ?)
                    """,
                    (project_id, actor_id, observation_id, json.dumps({"source": payload.get("source")}, sort_keys=True)),
                )
            data: dict[str, Any] = {"observation_id": observation_id}
            self._embed_observation(observation_id, text)
            self._refresh_core_memory(project_id, data)
            return data

        return self._idempotent_write(
            project_id=project_id,
            tool_name="store_observation",
            idempotency_key=self._optional_str(payload, "idempotency_key"),
            writer=run,
        )

    def _archive_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = self._require_str(payload, "project_id")
        title = self._require_str(payload, "title")
        content = self._require_str(payload, "content")
        actor_id = self._optional_str(payload, "user_id")
        self._enforce_user(actor_id)

        decision_props = {
            "problem_statement": self._require_str(payload, "problem_statement"),
            "decision": self._require_str(payload, "decision"),
            "alternatives_considered": self._require_list(payload, "alternatives_considered"),
            "tradeoffs": self._require_list(payload, "tradeoffs"),
            "status": self._require_str(payload, "status"),
        }

        def run() -> dict[str, Any]:
            node = self.repo.create_node(
                project_id=project_id,
                node_type="Decision",
                title=title,
                content=content,
                actor_id=actor_id,
            )
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO node_properties(node_id, key, value_json)
                    VALUES (?, 'decision_contract', ?)
                    """,
                    (node.node_id, json.dumps(decision_props, sort_keys=True)),
                )
            data: dict[str, Any] = {
                "node": self._node_response(node),
                "decision_contract": decision_props,
            }
            self._embed_node(node.node_id, title, content)
            self._refresh_core_memory(project_id, data)
            return data

        return self._idempotent_write(
            project_id=project_id,
            tool_name="archive_decision",
            idempotency_key=self._optional_str(payload, "idempotency_key"),
            writer=run,
        )

    def _get_project_summary(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = self._require_str(payload, "project_id")
        counts = self.conn.execute(
            """
            SELECT type, COUNT(*) AS count
            FROM nodes
            WHERE project_id = ? AND state != 'archived'
            GROUP BY type
            ORDER BY type
            """,
            (project_id,),
        ).fetchall()
        recent = self.conn.execute(
            """
            SELECT node_id, type, title, state, updated_at
            FROM nodes
            WHERE project_id = ? AND state IN ('active', 'deprecated')
            ORDER BY updated_at DESC
            LIMIT 5
            """,
            (project_id,),
        ).fetchall()
        return {
            "project_id": project_id,
            "counts": [{"type": row["type"], "count": row["count"]} for row in counts],
            "recent": [dict(row) for row in recent],
        }

    def _list_recent_incidents(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = self._require_str(payload, "project_id")
        limit = self._optional_int(payload, "limit") or 10
        rows = self.conn.execute(
            """
            SELECT node_id, title, content, state, version, updated_at
            FROM nodes
            WHERE project_id = ? AND type = 'Incident'
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (project_id, limit),
        ).fetchall()
        return {"incidents": [dict(row) for row in rows]}

    def _deprecate_node(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = self._require_str(payload, "project_id")
        node_id = self._require_str(payload, "node_id")
        expected_version = self._require_int(payload, "expected_version")
        actor_id = self._optional_str(payload, "user_id")
        self._enforce_user(actor_id)

        def run() -> dict[str, Any]:
            node = self.repo.get_node(node_id)
            if node.project_id != project_id:
                raise NotFoundError(f"node not found: {node_id}")
            updated = self.repo.update_node(
                node_id=node_id,
                expected_version=expected_version,
                actor_id=actor_id,
                state="deprecated",
            )
            data: dict[str, Any] = {"node": self._node_response(updated)}
            self._refresh_core_memory(project_id, data)
            return data

        return self._idempotent_write(
            project_id=project_id,
            tool_name="deprecate_node",
            idempotency_key=self._optional_str(payload, "idempotency_key"),
            writer=run,
        )

    def _idempotent_write(
        self,
        *,
        project_id: str,
        tool_name: str,
        idempotency_key: str | None,
        writer: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        if not idempotency_key:
            return writer()

        row = self.conn.execute(
            """
            SELECT response_json FROM idempotency_keys
            WHERE project_id = ? AND tool_name = ? AND idempotency_key = ?
            """,
            (project_id, tool_name, idempotency_key),
        ).fetchone()
        if row:
            data = json.loads(row["response_json"])
            data["_idempotent_replay"] = True
            return data

        data = writer()
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO idempotency_keys(project_id, tool_name, idempotency_key, response_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (project_id, tool_name, idempotency_key, json.dumps(data, sort_keys=True)),
                )
        except sqlite3.IntegrityError:
            row = self.conn.execute(
                """
                SELECT response_json FROM idempotency_keys
                WHERE project_id = ? AND tool_name = ? AND idempotency_key = ?
                """,
                (project_id, tool_name, idempotency_key),
            ).fetchone()
            if row:
                replay = json.loads(row["response_json"])
                replay["_idempotent_replay"] = True
                return replay
        return data

    def _compact_core_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = self._require_str(payload, "project_id")
        if self.core_materializer is None:
            raise ValidationError("core memory materializer not configured")
        result = self.core_materializer.refresh(project_id)
        return {"core_memory": result}

    def _refresh_core_memory(self, project_id: str, data: dict[str, Any]) -> None:
        if self.core_materializer is None:
            return
        result = self.core_materializer.refresh(project_id)
        data["core_memory_truncated"] = result["metadata"]["truncated"]

    def _extract_symbols(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = self._require_str(payload, "project_id")
        root_path = self._require_str(payload, "root_path")
        incremental = bool(payload.get("incremental", True))
        indexer = SymbolIndexer(self.conn)
        report = indexer.index_project(project_id=project_id, root=Path(root_path), incremental=incremental)
        return {
            "report": {
                "project_id": report.project_id,
                "scanned_files": report.scanned_files,
                "changed_files": report.changed_files,
                "symbols_added_or_updated": report.symbols_added_or_updated,
                "symbols_deprecated": report.symbols_deprecated,
                "dependencies_created": report.dependencies_created,
                "duration_ms": report.duration_ms,
            }
        }

    def _rebuild_embeddings(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = self._require_str(payload, "project_id")
        if self.embedding_worker is None:
            raise ValidationError("embedding worker not configured")
        count = self.embedding_worker.rebuild_node_embeddings(project_id)
        warnings: list[str] = []
        if not self.embedding_worker.available():
            warnings.append("EMBEDDING_DISABLED")
        return {"rebuild_count": count, "_warnings": warnings}

    def _rebuild_index_and_embeddings(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = self._require_str(payload, "project_id")
        root_path = self._require_str(payload, "root_path")
        indexer = SymbolIndexer(self.conn)
        report = indexer.index_project(project_id=project_id, root=Path(root_path), incremental=False)
        embedding_count = 0
        warnings: list[str] = []
        if self.embedding_worker is not None:
            embedding_count = self.embedding_worker.rebuild_node_embeddings(project_id)
            if not self.embedding_worker.available():
                warnings.append("EMBEDDING_DISABLED")
        return {
            "index_report": {
                "project_id": report.project_id,
                "scanned_files": report.scanned_files,
                "changed_files": report.changed_files,
                "symbols_added_or_updated": report.symbols_added_or_updated,
                "symbols_deprecated": report.symbols_deprecated,
                "dependencies_created": report.dependencies_created,
                "duration_ms": report.duration_ms,
            },
            "embedding_rebuild_count": embedding_count,
            "_warnings": warnings,
        }

    def _export_audit_log(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = self._require_str(payload, "project_id")
        limit = self._optional_int(payload, "limit") or 200
        if limit > 1000:
            raise ValidationError("limit must be <= 1000")
        since_ts = self._optional_str(payload, "since")
        where = "WHERE project_id = ?"
        params: list[Any] = [project_id]
        if since_ts:
            where += " AND created_at >= ?"
            params.append(since_ts)
        params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT event_id, project_id, actor_id, action, target_id, details_json, created_at
            FROM audit_events
            {where}
            ORDER BY event_id DESC
            LIMIT ?
            """
            ,
            tuple(params),
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            details = {}
            if row["details_json"]:
                try:
                    details = json.loads(row["details_json"])
                except json.JSONDecodeError:
                    details = {"raw": row["details_json"]}
            events.append(
                {
                    "event_id": row["event_id"],
                    "project_id": row["project_id"],
                    "actor_id": row["actor_id"],
                    "action": row["action"],
                    "target_id": row["target_id"],
                    "details": redact_sensitive(details),
                    "created_at": row["created_at"],
                }
            )
        canonical = "\n".join(json.dumps(evt, sort_keys=True) for evt in events)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return {"events": events, "integrity_sha256": digest}

    def _purge_observations(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = self._require_str(payload, "project_id")
        dry_run = bool(payload.get("dry_run", False))
        retention_days = self._optional_int(payload, "retention_days") or self.security.observation_retention_days
        if retention_days not in {7, 30, 90, 180}:
            raise ValidationError("retention_days must be one of 7, 30, 90, 180")
        actor_id = self._optional_str(payload, "user_id")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        candidates = self.conn.execute(
            """
            SELECT observation_id, text, confidence
            FROM observations
            WHERE project_id = ?
              AND promoted_node_id IS NULL
              AND needs_triage = 0
              AND created_at < ?
            """,
            (project_id, cutoff),
        ).fetchall()

        to_triage: list[str] = []
        to_delete: list[str] = []
        for row in candidates:
            is_high_value = (row["confidence"] is not None and row["confidence"] >= 0.8) or len(row["text"]) >= 320
            if is_high_value:
                to_triage.append(row["observation_id"])
            else:
                to_delete.append(row["observation_id"])

        if not dry_run:
            with self.conn:
                if to_triage:
                    marks = ",".join("?" for _ in to_triage)
                    self.conn.execute(
                        f"UPDATE observations SET needs_triage = 1 WHERE observation_id IN ({marks})",
                        tuple(to_triage),
                    )
                if to_delete:
                    marks = ",".join("?" for _ in to_delete)
                    self.conn.execute(
                        f"DELETE FROM observations WHERE observation_id IN ({marks})",
                        tuple(to_delete),
                    )
            self.repo.record_audit_event(
                project_id=project_id,
                actor_id=actor_id,
                action="observation.purge",
                target_id=project_id,
                details={
                    "retention_days": retention_days,
                    "dry_run": dry_run,
                    "triaged_count": len(to_triage),
                    "purged_count": len(to_delete),
                },
            )
        return {
            "retention_days": retention_days,
            "cutoff": cutoff,
            "triaged_observation_ids": to_triage,
            "purged_observation_ids": to_delete,
        }

    def _resolve_conflict(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = self._require_str(payload, "project_id")
        conflict_event_id = self._require_int(payload, "conflict_event_id")
        resolution_note = self._require_str(payload, "resolution_note")
        actor_id = self._optional_str(payload, "user_id")
        self._enforce_user(actor_id)
        node_id = self._optional_str(payload, "node_id")
        expected_version = self._optional_int(payload, "expected_version")

        def run() -> dict[str, Any]:
            row = self.conn.execute(
                """
                SELECT event_id, action, target_id
                FROM audit_events
                WHERE event_id = ? AND project_id = ?
                """,
                (conflict_event_id, project_id),
            ).fetchone()
            if row is None:
                raise ValidationError("conflict event not found")
            if not (row["action"] == "tool.conflict" or str(row["action"]).endswith(".conflict")):
                raise ValidationError("event is not a conflict record")

            updated_node: dict[str, Any] | None = None
            if node_id is not None:
                if expected_version is None:
                    raise ValidationError("expected_version is required when node_id is provided")
                node = self.repo.update_node(
                    node_id=node_id,
                    expected_version=expected_version,
                    actor_id=actor_id,
                    title=self._optional_str(payload, "title"),
                    content=self._optional_str(payload, "content"),
                    state=self._optional_str(payload, "state"),
                )
                updated_node = self._node_response(node)
                self._embed_node(node.node_id, node.title, node.content)

            self.repo.record_audit_event(
                project_id=project_id,
                actor_id=actor_id,
                action="conflict.resolve",
                target_id=str(conflict_event_id),
                details={
                    "resolution_note": resolution_note,
                    "node_id": node_id,
                    "expected_version": expected_version,
                },
            )
            result: dict[str, Any] = {
                "conflict_event_id": conflict_event_id,
                "resolution_note": resolution_note,
                "resolved": True,
            }
            if updated_node is not None:
                result["node"] = updated_node
                self._refresh_core_memory(project_id, result)
            return result

        return self._idempotent_write(
            project_id=project_id,
            tool_name="resolve_conflict",
            idempotency_key=self._optional_str(payload, "idempotency_key"),
            writer=run,
        )

    def _promote_branch_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = self._require_str(payload, "project_id")
        node_id = self._require_str(payload, "node_id")
        expected_version = self._require_int(payload, "expected_version")
        resolution_note = self._require_str(payload, "resolution_note")
        actor_id = self._optional_str(payload, "user_id")
        self._enforce_user(actor_id)

        def run() -> dict[str, Any]:
            node = self.repo.get_node(node_id)
            if node.project_id != project_id:
                raise NotFoundError(f"node not found: {node_id}")
            current_scope = "project"
            row = self.conn.execute(
                "SELECT value_json FROM node_properties WHERE node_id = ? AND key = 'scope'",
                (node_id,),
            ).fetchone()
            if row:
                try:
                    decoded = json.loads(row["value_json"])
                    if isinstance(decoded, str):
                        current_scope = decoded
                except json.JSONDecodeError:
                    current_scope = "project"
            if current_scope != "branch":
                raise ValidationError("node is not branch-scoped")

            updated = self.repo.update_node(
                node_id=node_id,
                expected_version=expected_version,
                actor_id=actor_id,
            )
            self.repo.upsert_node_property(node_id, "scope", "project")
            self.repo.record_audit_event(
                project_id=project_id,
                actor_id=actor_id,
                action="node.promote_scope",
                target_id=node_id,
                details={"from_scope": "branch", "to_scope": "project", "resolution_note": resolution_note},
            )
            data = {"node": self._node_response(updated), "scope": "project"}
            self._refresh_core_memory(project_id, data)
            return data

        return self._idempotent_write(
            project_id=project_id,
            tool_name="promote_branch_record",
            idempotency_key=self._optional_str(payload, "idempotency_key"),
            writer=run,
        )

    def _invalidate_stale_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = self._require_str(payload, "project_id")
        node_id = self._require_str(payload, "node_id")
        expected_version = self._require_int(payload, "expected_version")
        reason = self._require_str(payload, "reason")
        actor_id = self._optional_str(payload, "user_id")
        self._enforce_user(actor_id)
        corrected_node_id = self._optional_str(payload, "corrected_node_id")

        def run() -> dict[str, Any]:
            current = self.repo.get_node(node_id)
            if current.project_id != project_id:
                raise NotFoundError(f"node not found: {node_id}")
            first = self.repo.update_node(
                node_id=node_id,
                expected_version=expected_version,
                actor_id=actor_id,
                state="deprecated",
            )
            invalidated = self.repo.update_node(
                node_id=node_id,
                expected_version=first.version,
                actor_id=actor_id,
                state="invalidated",
            )
            self.repo.upsert_node_property(node_id, "invalidation_reason", reason)
            link: dict[str, Any] | None = None
            if corrected_node_id:
                edge = self.repo.create_edge(
                    project_id=project_id,
                    edge_type="DEPRECATES",
                    from_node_id=corrected_node_id,
                    to_node_id=node_id,
                    actor_id=actor_id,
                    weight=1.0,
                    state="active",
                )
                link = self._edge_response(edge)
            data: dict[str, Any] = {"node": self._node_response(invalidated), "reason": reason}
            if link is not None:
                data["link"] = link
            self._refresh_core_memory(project_id, data)
            return data

        return self._idempotent_write(
            project_id=project_id,
            tool_name="invalidate_stale_memory",
            idempotency_key=self._optional_str(payload, "idempotency_key"),
            writer=run,
        )

    def _validate_and_sanitize(
        self,
        tool_name: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        allowed_keys: dict[str, set[str]] = {
            "health": set(),
            "version": set(),
            "get_capabilities": set(),
            "get_metrics": set(),
            "create_entity": {
                "project_id",
                "type",
                "title",
                "content",
                "user_id",
                "state",
                "node_id",
                "idempotency_key",
            },
            "read_node": {"project_id", "node_id"},
            "update_entity": {
                "project_id",
                "node_id",
                "expected_version",
                "title",
                "content",
                "state",
                "user_id",
                "decision_rationale",
                "metadata_union",
                "idempotency_key",
            },
            "create_edge": {
                "project_id",
                "type",
                "from_node_id",
                "to_node_id",
                "weight",
                "state",
                "edge_id",
                "user_id",
                "idempotency_key",
            },
            "search_graph": {"project_id", "query", "limit", "include_deprecated"},
            "store_observation": {
                "project_id",
                "observation_id",
                "session_id",
                "text",
                "source",
                "confidence",
                "user_id",
                "idempotency_key",
            },
            "archive_decision": {
                "project_id",
                "title",
                "content",
                "problem_statement",
                "decision",
                "alternatives_considered",
                "tradeoffs",
                "status",
                "user_id",
                "idempotency_key",
            },
            "get_project_summary": {"project_id"},
            "list_recent_incidents": {"project_id", "limit"},
            "deprecate_node": {"project_id", "node_id", "expected_version", "user_id", "idempotency_key"},
            "compact_core_memory": {"project_id"},
            "extract_symbols": {"project_id", "root_path", "incremental"},
            "rebuild_embeddings": {"project_id"},
            "rebuild_index_and_embeddings": {"project_id", "root_path"},
            "export_audit_log": {"project_id", "limit", "since"},
            "purge_observations": {"project_id", "retention_days", "dry_run", "user_id"},
            "resolve_conflict": {
                "project_id",
                "conflict_event_id",
                "resolution_note",
                "node_id",
                "expected_version",
                "title",
                "content",
                "state",
                "user_id",
                "idempotency_key",
            },
            "promote_branch_record": {
                "project_id",
                "node_id",
                "expected_version",
                "resolution_note",
                "user_id",
                "idempotency_key",
            },
            "invalidate_stale_memory": {
                "project_id",
                "node_id",
                "expected_version",
                "reason",
                "corrected_node_id",
                "user_id",
                "idempotency_key",
            },
        }
        allowed = allowed_keys.get(tool_name, set())
        unknown = sorted(k for k in payload if k not in allowed)
        if unknown:
            raise ValidationError(f"unexpected fields for {tool_name}: {', '.join(unknown)}")

        sanitized_payload: dict[str, Any] = dict(payload)
        changed = False

        text_fields = {
            "title",
            "content",
            "query",
            "text",
            "problem_statement",
            "decision",
            "decision_rationale",
            "source",
            "status",
            "reason",
            "resolution_note",
        }
        for key, value in list(sanitized_payload.items()):
            if isinstance(value, str):
                if key in text_fields:
                    normalized, c = sanitize_text(value)
                else:
                    normalized = normalize_text(value)
                    c = normalized != value
                if key.endswith("_id") or key in {"project_id", "user_id", "idempotency_key"}:
                    if len(normalized) > self.security.max_id_chars:
                        raise ValidationError(f"{key} exceeds max length {self.security.max_id_chars}")
                if key == "query" and len(normalized) > self.security.max_query_chars:
                    raise ValidationError(f"query exceeds max length {self.security.max_query_chars}")
                if key == "text" and len(normalized) > self.security.max_observation_chars:
                    raise ValidationError(f"text exceeds max length {self.security.max_observation_chars}")
                sanitized_payload[key] = normalized
                changed = changed or c

        for key in ("limit",):
            if key in sanitized_payload and sanitized_payload[key] is not None:
                value = sanitized_payload[key]
                if not isinstance(value, int):
                    raise ValidationError(f"{key} must be an integer")
                if value < 1:
                    raise ValidationError(f"{key} must be >= 1")
        for key in ("include_deprecated", "incremental", "dry_run"):
            if key in sanitized_payload and sanitized_payload[key] is not None:
                if not isinstance(sanitized_payload[key], bool):
                    raise ValidationError(f"{key} must be a boolean")
        if "confidence" in sanitized_payload and sanitized_payload["confidence"] is not None:
            val = sanitized_payload["confidence"]
            if not isinstance(val, (int, float)):
                raise ValidationError("confidence must be a number")
            if val < 0.0 or val > 1.0:
                raise ValidationError("confidence must be between 0 and 1")
        if "weight" in sanitized_payload and sanitized_payload["weight"] is not None:
            val = sanitized_payload["weight"]
            if not isinstance(val, (int, float)):
                raise ValidationError("weight must be a number")
            if val < 0.0 or val > 1.0:
                raise ValidationError("weight must be between 0 and 1")
        if "expected_version" in sanitized_payload and sanitized_payload["expected_version"] is not None:
            if not isinstance(sanitized_payload["expected_version"], int):
                raise ValidationError("expected_version must be an integer")

        for key, max_len in (
            ("title", 200),
            ("content", 20000),
            ("problem_statement", 5000),
            ("decision", 5000),
            ("decision_rationale", 5000),
            ("source", 256),
            ("status", 64),
            ("reason", 1024),
            ("resolution_note", 1024),
            ("state", 32),
            ("type", 64),
        ):
            if key in sanitized_payload and isinstance(sanitized_payload[key], str):
                if len(sanitized_payload[key]) > max_len:
                    raise ValidationError(f"{key} exceeds max length {max_len}")

        if "metadata_union" in sanitized_payload and sanitized_payload["metadata_union"] is not None:
            union = sanitized_payload["metadata_union"]
            if not isinstance(union, dict):
                raise ValidationError("metadata_union must be an object")
            for mk in ("tags", "labels", "related_artifacts"):
                val = union.get(mk)
                if val is None:
                    continue
                if not isinstance(val, list):
                    raise ValidationError(f"metadata_union.{mk} must be a string array")
                if len(val) > self.security.metadata_items_limit:
                    raise ValidationError(
                        f"metadata_union.{mk} exceeds limit {self.security.metadata_items_limit}"
                    )
                if not all(isinstance(x, str) for x in val):
                    raise ValidationError(f"metadata_union.{mk} must be a string array")
                normalized_values = [normalize_text(x) for x in val]
                union[mk] = normalized_values
                changed = changed or normalized_values != val

        for k in ("alternatives_considered", "tradeoffs"):
            if k in sanitized_payload:
                val = sanitized_payload[k]
                if not isinstance(val, list):
                    raise ValidationError(f"{k} must be an array")
                if len(val) > 32:
                    raise ValidationError(f"{k} exceeds max item count 32")
                if not all(isinstance(x, str) for x in val):
                    raise ValidationError(f"{k} must be an array of strings")
                if any(len(normalize_text(x)) > 500 for x in val):
                    raise ValidationError(f"{k} entries must be <= 500 chars")
                normalized_values = [normalize_text(x) for x in val]
                sanitized_payload[k] = normalized_values
                changed = changed or normalized_values != val

        return sanitized_payload, changed

    def _apply_metadata_union(self, node_id: str, metadata_union: dict[str, Any]) -> None:
        for key in ("tags", "labels", "related_artifacts"):
            incoming = metadata_union.get(key)
            if incoming is None:
                continue
            if not isinstance(incoming, list) or not all(isinstance(x, str) for x in incoming):
                raise ValidationError(f"metadata_union.{key} must be a string array")
            row = self.conn.execute(
                "SELECT value_json FROM node_properties WHERE node_id = ? AND key = ?",
                (node_id, key),
            ).fetchone()
            existing: list[str] = []
            if row:
                try:
                    val = json.loads(row["value_json"])
                    if isinstance(val, list):
                        existing = [x for x in val if isinstance(x, str)]
                except json.JSONDecodeError:
                    existing = []
            merged = sorted(set(existing).union(incoming))
            self.repo.upsert_node_property(node_id, key, merged)

    def _embed_node(self, node_id: str, title: str, content: str) -> None:
        if self.embedding_worker is None:
            return
        self.embedding_worker.upsert_node_embedding(node_id, f"{title}\n{content}")

    def _embed_observation(self, observation_id: str, text: str) -> None:
        if self.embedding_worker is None:
            return
        self.embedding_worker.upsert_observation_embedding(observation_id, text)

    def _authorize(
        self,
        tool_name: str,
        payload: dict[str, Any],
        auth_context: AuthContext | None,
    ) -> None:
        if auth_context is None:
            return
        if not can_call_tool(auth_context.role, tool_name):
            raise PermissionError(f"role {auth_context.role} cannot call {tool_name}")
        if tool_name in {"health", "version", "get_capabilities", "get_metrics"}:
            payload["user_id"] = auth_context.user_id
            return
        project_id = payload.get("project_id")
        if not isinstance(project_id, str):
            raise PermissionError("project_id scope is required in team mode")
        if not is_project_allowed(auth_context, project_id):
            raise PermissionError("project_id is outside authorized scope")
        payload["user_id"] = auth_context.user_id

    def _rate_limit_actor(self, payload: dict[str, Any], auth_context: AuthContext | None) -> str:
        if auth_context is not None and auth_context.user_id:
            return auth_context.user_id
        uid = payload.get("user_id")
        if isinstance(uid, str) and uid:
            return uid
        return "anonymous"

    def _record_and_log(
        self,
        *,
        tool_name: str,
        trace_id: str,
        start: float,
        response: dict[str, Any],
        error_code: str | None,
    ) -> dict[str, Any]:
        duration_ms = (time.perf_counter() - start) * 1000.0
        self.metrics.record(tool_name, duration_ms=duration_ms, error_code=error_code)
        log_event(
            self.logger,
            "tool_call",
            trace_id=trace_id,
            tool=tool_name,
            duration_ms=round(duration_ms, 3),
            error_code=error_code,
            warnings=response.get("warnings", []),
        )
        alert = self.alert_pipeline.record(error=error_code is not None)
        if alert is not None:
            log_event(self.logger, "alert_triggered", **alert)
        return response

    def _record_conflict_audit(
        self,
        payload: dict[str, Any],
        auth_context: AuthContext | None,
        tool_name: str,
        message: str,
        details: dict[str, Any],
    ) -> None:
        project_id = payload.get("project_id")
        if not isinstance(project_id, str):
            return
        actor_id = auth_context.user_id if auth_context else self._optional_str(payload, "user_id")
        self.repo.record_audit_event(
            project_id=project_id,
            actor_id=actor_id,
            action="tool.conflict",
            target_id=tool_name,
            details={"message": message, "details": details},
        )

    def _emit_event(self, event: dict[str, Any]) -> None:
        if self.event_emitter is None:
            return
        self.event_emitter(event)

    def _enforce_user(self, user_id: str | None) -> None:
        if self.require_user_id and not user_id:
            raise PermissionError("user_id is required in team mode")

    def _node_response(self, node: Any) -> dict[str, Any]:
        record = asdict(node)
        record["type"] = record.pop("node_type")
        return record

    def _edge_response(self, edge: Any) -> dict[str, Any]:
        record = asdict(edge)
        record["type"] = record.pop("edge_type")
        return record

    def _require_str(self, payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{key} must be a non-empty string")
        return value

    def _optional_str(self, payload: dict[str, Any], key: str) -> str | None:
        value = payload.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValidationError(f"{key} must be a string")
        return value

    def _require_int(self, payload: dict[str, Any], key: str) -> int:
        value = payload.get(key)
        if not isinstance(value, int):
            raise ValidationError(f"{key} must be an integer")
        return value

    def _optional_int(self, payload: dict[str, Any], key: str) -> int | None:
        value = payload.get(key)
        if value is None:
            return None
        if not isinstance(value, int):
            raise ValidationError(f"{key} must be an integer")
        return value

    def _optional_float(self, payload: dict[str, Any], key: str) -> float | None:
        value = payload.get(key)
        if value is None:
            return None
        if not isinstance(value, (int, float)):
            raise ValidationError(f"{key} must be a number")
        return float(value)

    def _require_list(self, payload: dict[str, Any], key: str) -> list[Any]:
        value = payload.get(key)
        if not isinstance(value, list):
            raise ValidationError(f"{key} must be an array")
        return value
