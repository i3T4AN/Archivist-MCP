"""WebUI server for read and controlled-write workflows."""

from __future__ import annotations

import argparse
import json
import ssl
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from archivist_mcp.config import load_config
from archivist_mcp.db import connect
from archivist_mcp.memory.materializer import CoreMemoryMaterializer
from archivist_mcp.migrations.runner import run_migrations
from archivist_mcp.paths import MIGRATIONS_DIR
from archivist_mcp.reliability.recovery import recover_database_on_startup
from archivist_mcp.retrieval.embeddings import EmbeddingConfig as WorkerEmbeddingConfig
from archivist_mcp.retrieval.embeddings import EmbeddingWorker
from archivist_mcp.retrieval.hybrid import HybridRetrievalEngine, RetrievalWeights
from archivist_mcp.team.auth import AuthContext, can_call_tool, is_project_allowed, load_token_map
from archivist_mcp.observability.alerts import AlertConfig, AlertPipeline
from archivist_mcp.observability.logging import setup_structured_logger
from archivist_mcp.observability.rate_limit import RateLimiter
from archivist_mcp.tooling.server import ToolServer


class WebUiApp:
    def __init__(self, server: ToolServer, conn, *, tokens: dict[str, AuthContext], team_mode: bool):
        self.server = server
        self.conn = conn
        self.tokens = tokens
        self.team_mode = team_mode
        self._db_lock = threading.RLock()
        self.static_dir = Path(__file__).resolve().parent / "webui" / "static"

    def auth(self, headers: dict[str, str]) -> AuthContext | None:
        auth_header = headers.get("Authorization") or headers.get("authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return None
        token = auth_header.split(" ", 1)[1].strip()
        return self.tokens.get(token)

    def scope_error(self, project_id: str | None, ctx: AuthContext | None) -> str | None:
        if not self.team_mode:
            return None
        if ctx is None:
            return "unauthorized"
        if not isinstance(project_id, str):
            return "project_id required"
        if not is_project_allowed(ctx, project_id):
            return "project out of scope"
        return None

    def metrics_error(self, headers: dict[str, str]) -> str | None:
        if not self.team_mode:
            return None
        ctx = self.auth(headers)
        if ctx is None:
            return "unauthorized"
        if not can_call_tool(ctx.role, "get_metrics"):
            return "forbidden"
        return None

    def _record_webui_audit(
        self,
        *,
        project_id: str,
        actor_id: str | None,
        action: str,
        target_id: str,
        details: dict[str, Any],
    ) -> None:
        with self._db_lock:
            self.conn.execute(
                """
                INSERT INTO audit_events(project_id, actor_id, action, target_id, details_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (project_id, actor_id, action, target_id, json.dumps(details, sort_keys=True)),
            )

    def _status_for_tool_response(self, result: dict[str, Any]) -> int:
        if "error" not in result:
            return HTTPStatus.OK
        code = result["error"].get("code")
        if code == "AUTHZ_DENIED":
            return HTTPStatus.FORBIDDEN
        if code == "VALIDATION_ERROR":
            return HTTPStatus.BAD_REQUEST
        if code == "CONFLICT_ERROR":
            return HTTPStatus.CONFLICT
        if code == "NOT_FOUND":
            return HTTPStatus.NOT_FOUND
        if code == "RATE_LIMITED":
            return HTTPStatus.TOO_MANY_REQUESTS
        return HTTPStatus.INTERNAL_SERVER_ERROR

    def _run_write_tool(
        self,
        *,
        tool: str,
        payload: dict[str, Any],
        ctx: AuthContext | None,
        audit_action: str,
        target_id: str,
    ) -> tuple[int, dict[str, Any]]:
        result = self.server.handle_tool(tool, payload, auth_context=ctx)
        status = self._status_for_tool_response(result)
        if status == HTTPStatus.OK:
            actor = ctx.user_id if ctx else payload.get("user_id")
            self._record_webui_audit(
                project_id=payload["project_id"],
                actor_id=actor,
                action=audit_action,
                target_id=target_id,
                details={"tool": tool},
            )
            with self._db_lock:
                self.conn.commit()
        return status, result

    def api_search(self, project_id: str, query: str, limit: int, ctx: AuthContext | None) -> dict[str, Any]:
        return self.server.handle_tool(
            "search_graph",
            {"project_id": project_id, "query": query, "limit": limit},
            auth_context=ctx,
        )

    def api_project_summary(self, project_id: str, ctx: AuthContext | None) -> dict[str, Any]:
        return self.server.handle_tool(
            "get_project_summary",
            {"project_id": project_id},
            auth_context=ctx,
        )

    def api_graph(self, project_id: str, focus_node: str | None) -> dict[str, Any]:
        if focus_node:
            with self._db_lock:
                nrows = self.conn.execute(
                    "SELECT node_id, type, title, state, updated_at FROM nodes WHERE project_id = ? AND node_id IN (?, (SELECT from_node_id FROM edges WHERE project_id=? AND to_node_id=? LIMIT 1), (SELECT to_node_id FROM edges WHERE project_id=? AND from_node_id=? LIMIT 1))",
                    (project_id, focus_node, project_id, focus_node, project_id, focus_node),
                ).fetchall()
                erows = self.conn.execute(
                    "SELECT edge_id, type, from_node_id, to_node_id, state, weight FROM edges WHERE project_id = ? AND (from_node_id = ? OR to_node_id = ?) ORDER BY updated_at DESC LIMIT 120",
                    (project_id, focus_node, focus_node),
                ).fetchall()
        else:
            with self._db_lock:
                nrows = self.conn.execute(
                    "SELECT node_id, type, title, state, updated_at FROM nodes WHERE project_id = ? ORDER BY updated_at DESC LIMIT 120",
                    (project_id,),
                ).fetchall()
                erows = self.conn.execute(
                    "SELECT edge_id, type, from_node_id, to_node_id, state, weight FROM edges WHERE project_id = ? ORDER BY updated_at DESC LIMIT 240",
                    (project_id,),
                ).fetchall()
        return {
            "nodes": [dict(r) for r in nrows],
            "edges": [dict(r) for r in erows],
        }

    def api_decisions(self, project_id: str) -> dict[str, Any]:
        with self._db_lock:
            rows = self.conn.execute(
                "SELECT node_id, title, content, state, version, updated_at FROM nodes WHERE project_id = ? AND type = 'Decision' ORDER BY updated_at DESC LIMIT 100",
                (project_id,),
            ).fetchall()
        return {"decisions": [dict(r) for r in rows]}

    def api_incidents(self, project_id: str) -> dict[str, Any]:
        with self._db_lock:
            rows = self.conn.execute(
                """
                SELECT i.node_id, i.title, i.content, i.state, i.version, i.updated_at,
                       d.node_id AS resolved_by_id, d.title AS resolved_by_title
                FROM nodes i
                LEFT JOIN edges e ON e.project_id = i.project_id AND e.from_node_id = i.node_id AND e.type = 'RESOLVED_BY' AND e.state = 'active'
                LEFT JOIN nodes d ON d.node_id = e.to_node_id
                WHERE i.project_id = ? AND i.type = 'Incident'
                ORDER BY i.updated_at DESC
                LIMIT 100
                """,
                (project_id,),
            ).fetchall()
        return {"incidents": [dict(r) for r in rows]}

    def api_conflicts(self, project_id: str) -> dict[str, Any]:
        with self._db_lock:
            rows = self.conn.execute(
                """
                SELECT event_id, actor_id, action, target_id, details_json, created_at
                FROM audit_events
                WHERE project_id = ?
                  AND (action = 'tool.conflict' OR action LIKE '%.conflict')
                ORDER BY event_id DESC
                LIMIT 200
                """,
                (project_id,),
            ).fetchall()
        out = []
        for r in rows:
            details: dict[str, Any] = {}
            if r["details_json"]:
                try:
                    details = json.loads(r["details_json"])
                except json.JSONDecodeError:
                    details = {"raw": r["details_json"]}
            out.append(
                {
                    "event_id": r["event_id"],
                    "actor_id": r["actor_id"],
                    "action": r["action"],
                    "target_id": r["target_id"],
                    "details": details,
                    "created_at": r["created_at"],
                }
            )
        return {"conflicts": out}

    def api_rule_write(
        self,
        payload: dict[str, Any],
        ctx: AuthContext | None,
    ) -> tuple[int, dict[str, Any]]:
        action = payload.get("action")
        project_id = payload.get("project_id")
        if not isinstance(project_id, str):
            return HTTPStatus.BAD_REQUEST, {"error": {"code": "VALIDATION_ERROR", "message": "project_id is required", "details": {}}}

        if action == "create":
            severity = payload.get("severity")
            enforcement = payload.get("enforcement")
            if not isinstance(severity, str) or not isinstance(enforcement, str):
                return HTTPStatus.BAD_REQUEST, {"error": {"code": "VALIDATION_ERROR", "message": "severity and enforcement are required", "details": {}}}
            status, out = self._run_write_tool(
                tool="create_entity",
                payload={
                    "project_id": project_id,
                    "type": "Rule",
                    "title": payload.get("title", ""),
                    "content": payload.get("content", ""),
                    "idempotency_key": payload.get("idempotency_key"),
                    **({"user_id": payload.get("user_id")} if payload.get("user_id") else {}),
                },
                ctx=ctx,
                audit_action="webui.rule.create",
                target_id="Rule",
            )
            if status == HTTPStatus.OK:
                node_id = out["data"]["node"]["node_id"]
                self.server.repo.upsert_node_property(
                    node_id,
                    "rule_contract",
                    {
                        "severity": severity,
                        "enforcement": enforcement,
                        "scope": payload.get("scope", "project"),
                    },
                )
                self.server.repo.upsert_node_property(node_id, "scope", payload.get("scope", "project"))
                with self._db_lock:
                    self.conn.commit()
            return status, out

        if action == "update":
            status, out = self._run_write_tool(
                tool="update_entity",
                payload={
                    "project_id": project_id,
                    "node_id": payload.get("node_id"),
                    "expected_version": payload.get("expected_version"),
                    "title": payload.get("title"),
                    "content": payload.get("content"),
                    "idempotency_key": payload.get("idempotency_key"),
                    **({"user_id": payload.get("user_id")} if payload.get("user_id") else {}),
                },
                ctx=ctx,
                audit_action="webui.rule.update",
                target_id=str(payload.get("node_id")),
            )
            return status, out

        if action == "deprecate":
            if payload.get("confirm") is not True:
                return HTTPStatus.BAD_REQUEST, {"error": {"code": "VALIDATION_ERROR", "message": "confirm=true required", "details": {}}}
            status, out = self._run_write_tool(
                tool="deprecate_node",
                payload={
                    "project_id": project_id,
                    "node_id": payload.get("node_id"),
                    "expected_version": payload.get("expected_version"),
                    "idempotency_key": payload.get("idempotency_key"),
                    **({"user_id": payload.get("user_id")} if payload.get("user_id") else {}),
                },
                ctx=ctx,
                audit_action="webui.rule.deprecate",
                target_id=str(payload.get("node_id")),
            )
            return status, out

        return HTTPStatus.BAD_REQUEST, {"error": {"code": "VALIDATION_ERROR", "message": "unknown action", "details": {}}}

    def api_conflict_resolve(self, payload: dict[str, Any], ctx: AuthContext | None) -> tuple[int, dict[str, Any]]:
        if payload.get("confirm") is not True:
            return HTTPStatus.BAD_REQUEST, {"error": {"code": "VALIDATION_ERROR", "message": "confirm=true required", "details": {}}}
        status, out = self._run_write_tool(
            tool="resolve_conflict",
            payload={
                "project_id": payload.get("project_id"),
                "conflict_event_id": payload.get("conflict_event_id"),
                "resolution_note": payload.get("resolution_note"),
                "node_id": payload.get("node_id"),
                "expected_version": payload.get("expected_version"),
                "title": payload.get("title"),
                "content": payload.get("content"),
                "state": payload.get("state"),
                "idempotency_key": payload.get("idempotency_key"),
                **({"user_id": payload.get("user_id")} if payload.get("user_id") else {}),
            },
            ctx=ctx,
            audit_action="webui.conflict.resolve",
            target_id=str(payload.get("conflict_event_id")),
        )
        return status, out

    def api_promote_scope(self, payload: dict[str, Any], ctx: AuthContext | None) -> tuple[int, dict[str, Any]]:
        if payload.get("confirm") is not True:
            return HTTPStatus.BAD_REQUEST, {"error": {"code": "VALIDATION_ERROR", "message": "confirm=true required", "details": {}}}
        status, out = self._run_write_tool(
            tool="promote_branch_record",
            payload={
                "project_id": payload.get("project_id"),
                "node_id": payload.get("node_id"),
                "expected_version": payload.get("expected_version"),
                "resolution_note": payload.get("resolution_note"),
                "idempotency_key": payload.get("idempotency_key"),
                **({"user_id": payload.get("user_id")} if payload.get("user_id") else {}),
            },
            ctx=ctx,
            audit_action="webui.scope.promote",
            target_id=str(payload.get("node_id")),
        )
        return status, out

    def api_invalidate_memory(self, payload: dict[str, Any], ctx: AuthContext | None) -> tuple[int, dict[str, Any]]:
        if payload.get("confirm") is not True:
            return HTTPStatus.BAD_REQUEST, {"error": {"code": "VALIDATION_ERROR", "message": "confirm=true required", "details": {}}}
        status, out = self._run_write_tool(
            tool="invalidate_stale_memory",
            payload={
                "project_id": payload.get("project_id"),
                "node_id": payload.get("node_id"),
                "expected_version": payload.get("expected_version"),
                "reason": payload.get("reason"),
                "corrected_node_id": payload.get("corrected_node_id"),
                "idempotency_key": payload.get("idempotency_key"),
                **({"user_id": payload.get("user_id")} if payload.get("user_id") else {}),
            },
            ctx=ctx,
            audit_action="webui.memory.invalidate",
            target_id=str(payload.get("node_id")),
        )
        return status, out


def _json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _text(handler: BaseHTTPRequestHandler, status: int, content: bytes, content_type: str) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(content)))
    handler.end_headers()
    handler.wfile.write(content)


def make_handler(app: WebUiApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ArchivistWebUI/0.2"

        def _project_and_ctx(self) -> tuple[str | None, AuthContext | None]:
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            project_id = params.get("project_id", [None])[0]
            ctx = app.auth(dict(self.headers.items()))
            err = app.scope_error(project_id, ctx)
            if err:
                status = (
                    HTTPStatus.UNAUTHORIZED
                    if err == "unauthorized"
                    else HTTPStatus.FORBIDDEN
                    if err == "project out of scope"
                    else HTTPStatus.BAD_REQUEST
                )
                _json(self, status, {"error": err})
                return None, None
            return project_id, ctx

        def _project_and_ctx_for_body(self, body: dict[str, Any]) -> tuple[str | None, AuthContext | None]:
            project_id = body.get("project_id")
            ctx = app.auth(dict(self.headers.items()))
            err = app.scope_error(project_id, ctx)
            if err:
                status = (
                    HTTPStatus.UNAUTHORIZED
                    if err == "unauthorized"
                    else HTTPStatus.FORBIDDEN
                    if err == "project out of scope"
                    else HTTPStatus.BAD_REQUEST
                )
                _json(self, status, {"error": err})
                return None, None
            return project_id if isinstance(project_id, str) else None, ctx

        def _serve_static(self, path: str) -> None:
            if path == "/" or path == "":
                target = app.static_dir / "index.html"
                ctype = "text/html; charset=utf-8"
            elif path.startswith("/static/"):
                rel = path[len("/static/") :]
                if ".." in rel or rel.startswith("/"):
                    _json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                target = app.static_dir / rel
                if rel.endswith(".js"):
                    ctype = "application/javascript; charset=utf-8"
                elif rel.endswith(".css"):
                    ctype = "text/css; charset=utf-8"
                else:
                    ctype = "application/octet-stream"
            else:
                target = app.static_dir / "index.html"
                ctype = "text/html; charset=utf-8"

            if not target.exists() or not target.is_file():
                _json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            _text(self, HTTPStatus.OK, target.read_bytes(), ctype)

        def do_GET(self) -> None:              
            parsed = urlparse(self.path)
            if parsed.path == "/metrics":
                err = app.metrics_error(dict(self.headers.items()))
                if err == "unauthorized":
                    _json(self, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if err == "forbidden":
                    _json(self, HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                    return
                _text(
                    self,
                    HTTPStatus.OK,
                    app.server.metrics.render_prometheus().encode("utf-8"),
                    "text/plain; version=0.0.4; charset=utf-8",
                )
                return
            if not parsed.path.startswith("/api/"):
                self._serve_static(parsed.path)
                return

            project_id, ctx = self._project_and_ctx()
            if app.team_mode and ctx is None:
                return

            if parsed.path == "/api/search":
                params = parse_qs(parsed.query)
                q = params.get("q", [""])[0]
                limit = int(params.get("limit", ["8"])[0])
                if not isinstance(project_id, str):
                    _json(self, HTTPStatus.BAD_REQUEST, {"error": "project_id required"})
                    return
                _json(self, HTTPStatus.OK, app.api_search(project_id, q, limit, ctx))
                return

            if parsed.path == "/api/project_summary":
                if not isinstance(project_id, str):
                    _json(self, HTTPStatus.BAD_REQUEST, {"error": "project_id required"})
                    return
                _json(self, HTTPStatus.OK, app.api_project_summary(project_id, ctx))
                return

            if parsed.path == "/api/graph":
                if not isinstance(project_id, str):
                    _json(self, HTTPStatus.BAD_REQUEST, {"error": "project_id required"})
                    return
                params = parse_qs(parsed.query)
                focus_node = params.get("node_id", [None])[0]
                _json(self, HTTPStatus.OK, app.api_graph(project_id, focus_node))
                return

            if parsed.path == "/api/decisions":
                if not isinstance(project_id, str):
                    _json(self, HTTPStatus.BAD_REQUEST, {"error": "project_id required"})
                    return
                _json(self, HTTPStatus.OK, app.api_decisions(project_id))
                return

            if parsed.path == "/api/incidents":
                if not isinstance(project_id, str):
                    _json(self, HTTPStatus.BAD_REQUEST, {"error": "project_id required"})
                    return
                _json(self, HTTPStatus.OK, app.api_incidents(project_id))
                return

            if parsed.path == "/api/conflicts":
                if not isinstance(project_id, str):
                    _json(self, HTTPStatus.BAD_REQUEST, {"error": "project_id required"})
                    return
                _json(self, HTTPStatus.OK, app.api_conflicts(project_id))
                return

            _json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:              
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                _json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                _json(self, HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
                return

            project_id, ctx = self._project_and_ctx_for_body(body)
            if app.team_mode and ctx is None:
                return
            if not isinstance(project_id, str):
                _json(self, HTTPStatus.BAD_REQUEST, {"error": "project_id required"})
                return

            if parsed.path == "/api/rules":
                status, out = app.api_rule_write(body, ctx)
                _json(self, status, out)
                return
            if parsed.path == "/api/conflicts/resolve":
                status, out = app.api_conflict_resolve(body, ctx)
                _json(self, status, out)
                return
            if parsed.path == "/api/promote_scope":
                status, out = app.api_promote_scope(body, ctx)
                _json(self, status, out)
                return
            if parsed.path == "/api/invalidate":
                status, out = app.api_invalidate_memory(body, ctx)
                _json(self, status, out)
                return

            _json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

        def log_message(self, fmt: str, *args) -> None:
            return

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=".archivist/archivist.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--team-mode", action="store_true")
    parser.add_argument("--tls-cert")
    parser.add_argument("--tls-key")
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
        check_same_thread=False,
    )
    run_migrations(conn, MIGRATIONS_DIR)

    core_dir = db_path.resolve().parent
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
        require_user_id=args.team_mode,
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
        logger=setup_structured_logger("archivist.webui")
        if config.observability.structured_logging
        else None,
        enable_experimental_tools=config.feature_flags.experimental_tools_enabled,
        disabled_tools=set(config.feature_flags.disabled_tools),
    )

    try:
        tokens = load_token_map()
    except ValueError as exc:
        raise SystemExit(f"invalid ARCHIVIST_SSE_TOKENS: {exc}") from exc
    team_mode = args.team_mode or bool(tokens)
    app = WebUiApp(server, conn, tokens=tokens, team_mode=team_mode)
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    tls_enabled = config.tls.enabled or bool(args.tls_cert) or bool(args.tls_key)
    tls_cert = args.tls_cert or config.tls.cert_file
    tls_key = args.tls_key or config.tls.key_file
    if tls_enabled:
        if not tls_cert or not tls_key:
            raise SystemExit("TLS enabled but cert/key file missing")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=tls_cert, keyfile=tls_key)
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    scheme = "https" if tls_enabled else "http"
    print(f"WebUI listening on {scheme}://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        conn.close()


if __name__ == "__main__":
    main()
