"""JSON-RPC MCP stdio adapter for Archivist tool calls."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from archivist_mcp.config import load_config
from archivist_mcp.db import connect
from archivist_mcp.memory.materializer import CoreMemoryMaterializer
from archivist_mcp.migrations.runner import run_migrations
from archivist_mcp.paths import MIGRATIONS_DIR
from archivist_mcp.reliability.recovery import recover_database_on_startup
from archivist_mcp.retrieval.embeddings import EmbeddingConfig as WorkerEmbeddingConfig
from archivist_mcp.retrieval.embeddings import EmbeddingWorker
from archivist_mcp.retrieval.hybrid import HybridRetrievalEngine, RetrievalWeights
from archivist_mcp.team.auth import AuthContext
from archivist_mcp.observability.alerts import AlertConfig, AlertPipeline
from archivist_mcp.observability.logging import setup_structured_logger
from archivist_mcp.observability.rate_limit import RateLimiter
from archivist_mcp.tooling.server import ToolServer

SERVER_NAME = "archivist-mcp"
SERVER_VERSION = "0.1.0-rc1"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

TOOL_DESCRIPTIONS: dict[str, str] = {
    "health": "Basic liveness check for the Archivist server.",
    "version": "Return server and envelope version identifiers.",
    "get_capabilities": "Return supported tools and error taxonomy.",
    "get_metrics": "Return in-memory request/error/latency metrics snapshot.",
    "create_entity": "Create a project-scoped node/entity record.",
    "read_node": "Read a node and its properties by id.",
    "update_entity": "Update a node with optimistic version checks.",
    "create_edge": "Create a typed relationship between two nodes.",
    "search_graph": "Hybrid retrieval over archived project memory.",
    "store_observation": "Store a raw observation fact for later triage.",
    "archive_decision": "Create a Decision node with decision-contract fields.",
    "get_project_summary": "Get node counts and recent node activity by project.",
    "list_recent_incidents": "List incident nodes for a project.",
    "deprecate_node": "Transition a node to deprecated state with version checks.",
    "compact_core_memory": "Refresh compact core memory outputs for a project.",
    "extract_symbols": "Index code symbols and dependencies for a repository path.",
    "rebuild_embeddings": "Rebuild node embeddings for a project.",
    "rebuild_index_and_embeddings": "Full symbol reindex followed by embedding rebuild.",
    "export_audit_log": "Export redacted audit events plus integrity digest.",
    "purge_observations": "Apply retention policy to stale observations.",
    "resolve_conflict": "Resolve a previously recorded conflict event.",
    "promote_branch_record": "Promote a branch-scoped record to project scope.",
    "invalidate_stale_memory": "Invalidate stale memory and optionally link a correction.",
}

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "health": {"type": "object", "properties": {}, "additionalProperties": False},
    "version": {"type": "object", "properties": {}, "additionalProperties": False},
    "get_capabilities": {"type": "object", "properties": {}, "additionalProperties": False},
    "get_metrics": {"type": "object", "properties": {}, "additionalProperties": False},
    "create_entity": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "type": {"type": "string"},
            "title": {"type": "string"},
            "content": {"type": "string"},
            "user_id": {"type": "string"},
            "state": {"type": "string"},
            "node_id": {"type": "string"},
            "idempotency_key": {"type": "string"},
        },
        "required": ["project_id", "type", "title", "content"],
        "additionalProperties": False,
    },
    "read_node": {
        "type": "object",
        "properties": {"project_id": {"type": "string"}, "node_id": {"type": "string"}},
        "required": ["project_id", "node_id"],
        "additionalProperties": False,
    },
    "update_entity": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "node_id": {"type": "string"},
            "expected_version": {"type": "integer"},
            "title": {"type": "string"},
            "content": {"type": "string"},
            "state": {"type": "string"},
            "user_id": {"type": "string"},
            "decision_rationale": {"type": "string"},
            "metadata_union": {"type": "object"},
            "idempotency_key": {"type": "string"},
        },
        "required": ["project_id", "node_id", "expected_version"],
        "additionalProperties": False,
    },
    "create_edge": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "type": {"type": "string"},
            "from_node_id": {"type": "string"},
            "to_node_id": {"type": "string"},
            "weight": {"type": "number"},
            "state": {"type": "string"},
            "edge_id": {"type": "string"},
            "user_id": {"type": "string"},
            "idempotency_key": {"type": "string"},
        },
        "required": ["project_id", "type", "from_node_id", "to_node_id"],
        "additionalProperties": False,
    },
    "search_graph": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "include_deprecated": {"type": "boolean"},
        },
        "required": ["project_id", "query"],
        "additionalProperties": False,
    },
    "store_observation": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "observation_id": {"type": "string"},
            "session_id": {"type": "string"},
            "text": {"type": "string"},
            "source": {"type": "string"},
            "confidence": {"type": "number"},
            "user_id": {"type": "string"},
            "idempotency_key": {"type": "string"},
        },
        "required": ["project_id", "text"],
        "additionalProperties": False,
    },
    "archive_decision": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "title": {"type": "string"},
            "content": {"type": "string"},
            "problem_statement": {"type": "string"},
            "decision": {"type": "string"},
            "alternatives_considered": {"type": "array", "items": {"type": "string"}},
            "tradeoffs": {"type": "array", "items": {"type": "string"}},
            "status": {"type": "string"},
            "user_id": {"type": "string"},
            "idempotency_key": {"type": "string"},
        },
        "required": [
            "project_id",
            "title",
            "content",
            "problem_statement",
            "decision",
            "alternatives_considered",
            "tradeoffs",
            "status",
        ],
        "additionalProperties": False,
    },
    "get_project_summary": {
        "type": "object",
        "properties": {"project_id": {"type": "string"}},
        "required": ["project_id"],
        "additionalProperties": False,
    },
    "list_recent_incidents": {
        "type": "object",
        "properties": {"project_id": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["project_id"],
        "additionalProperties": False,
    },
    "deprecate_node": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "node_id": {"type": "string"},
            "expected_version": {"type": "integer"},
            "user_id": {"type": "string"},
            "idempotency_key": {"type": "string"},
        },
        "required": ["project_id", "node_id", "expected_version"],
        "additionalProperties": False,
    },
    "compact_core_memory": {
        "type": "object",
        "properties": {"project_id": {"type": "string"}},
        "required": ["project_id"],
        "additionalProperties": False,
    },
    "extract_symbols": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "root_path": {"type": "string"},
            "incremental": {"type": "boolean"},
        },
        "required": ["project_id", "root_path"],
        "additionalProperties": False,
    },
    "rebuild_embeddings": {
        "type": "object",
        "properties": {"project_id": {"type": "string"}},
        "required": ["project_id"],
        "additionalProperties": False,
    },
    "rebuild_index_and_embeddings": {
        "type": "object",
        "properties": {"project_id": {"type": "string"}, "root_path": {"type": "string"}},
        "required": ["project_id", "root_path"],
        "additionalProperties": False,
    },
    "export_audit_log": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "limit": {"type": "integer"},
            "since": {"type": "string"},
        },
        "required": ["project_id"],
        "additionalProperties": False,
    },
    "purge_observations": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "retention_days": {"type": "integer"},
            "dry_run": {"type": "boolean"},
            "user_id": {"type": "string"},
        },
        "required": ["project_id"],
        "additionalProperties": False,
    },
    "resolve_conflict": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "conflict_event_id": {"type": "integer"},
            "resolution_note": {"type": "string"},
            "node_id": {"type": "string"},
            "expected_version": {"type": "integer"},
            "title": {"type": "string"},
            "content": {"type": "string"},
            "state": {"type": "string"},
            "user_id": {"type": "string"},
            "idempotency_key": {"type": "string"},
        },
        "required": ["project_id", "conflict_event_id", "resolution_note"],
        "additionalProperties": False,
    },
    "promote_branch_record": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "node_id": {"type": "string"},
            "expected_version": {"type": "integer"},
            "resolution_note": {"type": "string"},
            "user_id": {"type": "string"},
            "idempotency_key": {"type": "string"},
        },
        "required": ["project_id", "node_id", "expected_version", "resolution_note"],
        "additionalProperties": False,
    },
    "invalidate_stale_memory": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "node_id": {"type": "string"},
            "expected_version": {"type": "integer"},
            "reason": {"type": "string"},
            "corrected_node_id": {"type": "string"},
            "user_id": {"type": "string"},
            "idempotency_key": {"type": "string"},
        },
        "required": ["project_id", "node_id", "expected_version", "reason"],
        "additionalProperties": False,
    },
}


def _jsonrpc_error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def _jsonrpc_result(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "result": result,
    }


def _read_message(stdin: Any) -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        decoded = line.decode("utf-8", errors="replace")
        if ":" not in decoded:
            continue
        name, value = decoded.split(":", 1)
        headers[name.strip().lower()] = value.strip()

    length_str = headers.get("content-length")
    if length_str is None:
        return None
    try:
        length = int(length_str)
    except ValueError:
        return None
    if length < 0:
        return None

    payload = stdin.buffer.read(length)
    if len(payload) != length:
        return None
    return json.loads(payload.decode("utf-8"))


def _write_message(stdout: Any, message: dict[str, Any]) -> None:
    raw = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    stdout.buffer.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii"))
    stdout.buffer.write(raw)
    stdout.buffer.flush()


def _tool_specs(server: ToolServer) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for name in server.list_tools():
        specs.append(
            {
                "name": name,
                "description": TOOL_DESCRIPTIONS.get(name, f"Archivist tool: {name}"),
                "inputSchema": TOOL_SCHEMAS.get(
                    name,
                    {"type": "object", "properties": {}, "additionalProperties": True},
                ),
            }
        )
    return specs


def _tool_call_result(
    server: ToolServer,
    tool_name: str,
    arguments: dict[str, Any],
    auth_context: AuthContext | None = None,
) -> dict[str, Any]:
    out = server.handle_tool(tool_name, arguments, auth_context=auth_context)
    if "error" in out:
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(out["error"], sort_keys=True),
                }
            ],
            "structuredContent": out,
            "isError": True,
        }
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(out["data"], sort_keys=True),
            }
        ],
        "structuredContent": out["data"],
        "_meta": {
            "trace_id": out["trace_id"],
            "warnings": out["warnings"],
            "envelope_version": out["version"],
        },
        "isError": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=".archivist/archivist.db")
    parser.add_argument("--require-user-id", action="store_true")
    args = parser.parse_args()

    config = load_config()
    db_path = Path(args.db)
    if config.reliability.startup_integrity_check and db_path.exists():
        recover_database_on_startup(
            db_path,
            Path(config.reliability.snapshot_dir),
            config.reliability.auto_restore_on_corruption,
            encryption_key=config.security.encryption_key,
        )

    conn = connect(
        str(db_path),
        encryption_key=config.security.encryption_key,
        encryption_required=config.security.encryption_required,
    )
    run_migrations(conn, MIGRATIONS_DIR)

    core_dir = Path(args.db).resolve().parent
    materializer = CoreMemoryMaterializer(
        conn,
        output_dir=core_dir,
        core_max_kb=config.memory.core_max_kb,
    )
    embedding_worker = EmbeddingWorker(
        conn,
        WorkerEmbeddingConfig(
            enabled=config.embedding.enabled,
            provider=config.embedding.provider,
            model=config.embedding.model,
            dimensions=config.embedding.dimensions,
            offline_strict=config.embedding.offline_strict,
        ),
    )
    retrieval_engine = HybridRetrievalEngine(
        conn,
        embedding_worker,
        RetrievalWeights(
            fts_weight=config.retrieval.fts_weight,
            vector_weight=config.retrieval.vector_weight,
            graph_weight=config.retrieval.graph_weight,
            recency_weight=config.retrieval.recency_weight,
        ),
    )
    server = ToolServer(
        conn,
        require_user_id=args.require_user_id,
        core_materializer=materializer,
        embedding_worker=embedding_worker,
        retrieval_engine=retrieval_engine,
        security=config.security,
        rate_limiter=RateLimiter(
            enabled=config.rate_limit.enabled,
            per_actor_per_minute=config.rate_limit.per_actor_per_minute,
        ),
        alert_pipeline=AlertPipeline(
            AlertConfig(
                enabled=config.observability.alert_enabled,
                min_calls=config.observability.alert_min_calls,
                error_rate_threshold=config.observability.alert_error_rate_threshold,
                cooldown_seconds=config.observability.alert_cooldown_seconds,
            )
        ),
        logger=setup_structured_logger("archivist.mcp_stdio")
        if config.observability.structured_logging
        else None,
        enable_experimental_tools=config.feature_flags.experimental_tools_enabled,
        disabled_tools=set(config.feature_flags.disabled_tools),
    )

    while True:
        try:
            msg = _read_message(sys.stdin)
        except json.JSONDecodeError:
            _write_message(sys.stdout, _jsonrpc_error(None, -32700, "Parse error"))
            continue
        except Exception:
            _write_message(sys.stdout, _jsonrpc_error(None, -32000, "Transport read failure"))
            continue

        if msg is None:
            break
        if not isinstance(msg, dict):
            _write_message(sys.stdout, _jsonrpc_error(None, -32600, "Invalid Request"))
            continue

        method = msg.get("method")
        message_id = msg.get("id")
        params = msg.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if message_id is not None:
                _write_message(sys.stdout, _jsonrpc_error(message_id, -32602, "Invalid params"))
            continue

                                     
        if message_id is None and isinstance(method, str) and method.startswith("notifications/"):
            continue

        if method == "initialize":
            protocol_version = params.get("protocolVersion")
            if not isinstance(protocol_version, str):
                protocol_version = DEFAULT_PROTOCOL_VERSION
            _write_message(
                sys.stdout,
                _jsonrpc_result(
                    message_id,
                    {
                        "protocolVersion": protocol_version,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    },
                ),
            )
            continue

        if method == "ping":
            _write_message(sys.stdout, _jsonrpc_result(message_id, {}))
            continue

        if method == "tools/list":
            _write_message(
                sys.stdout,
                _jsonrpc_result(
                    message_id,
                    {
                        "tools": _tool_specs(server),
                    },
                ),
            )
            continue

        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(tool_name, str):
                _write_message(sys.stdout, _jsonrpc_error(message_id, -32602, "name must be a string"))
                continue
            if not isinstance(arguments, dict):
                _write_message(sys.stdout, _jsonrpc_error(message_id, -32602, "arguments must be an object"))
                continue
            result = _tool_call_result(server, tool_name, arguments)
            _write_message(sys.stdout, _jsonrpc_result(message_id, result))
            continue

        if isinstance(method, str) and method.startswith("notifications/"):
            continue

        if message_id is not None:
            _write_message(sys.stdout, _jsonrpc_error(message_id, -32601, "Method not found"))

    conn.close()


if __name__ == "__main__":
    main()
