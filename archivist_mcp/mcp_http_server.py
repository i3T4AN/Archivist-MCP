"""JSON-RPC MCP HTTP transport for Archivist tool calls."""

from __future__ import annotations

import argparse
import json
import ssl
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from archivist_mcp.config import load_config
from archivist_mcp.db import connect
from archivist_mcp.mcp_stdio_server import (
    DEFAULT_PROTOCOL_VERSION,
    SERVER_NAME,
    SERVER_VERSION,
    TOOL_DESCRIPTIONS,
    TOOL_SCHEMAS,
    _jsonrpc_error,
    _jsonrpc_result,
    _tool_call_result,
)
from archivist_mcp.memory.materializer import CoreMemoryMaterializer
from archivist_mcp.migrations.runner import run_migrations
from archivist_mcp.paths import MIGRATIONS_DIR
from archivist_mcp.reliability.recovery import recover_database_on_startup
from archivist_mcp.retrieval.embeddings import EmbeddingConfig as WorkerEmbeddingConfig
from archivist_mcp.retrieval.embeddings import EmbeddingWorker
from archivist_mcp.retrieval.hybrid import HybridRetrievalEngine, RetrievalWeights
from archivist_mcp.team.auth import AuthContext, can_call_tool, load_token_map
from archivist_mcp.observability.alerts import AlertConfig, AlertPipeline
from archivist_mcp.observability.logging import setup_structured_logger
from archivist_mcp.observability.rate_limit import RateLimiter
from archivist_mcp.tooling.server import ToolServer


class McpHttpApp:
    def __init__(self, server: ToolServer, tokens: dict[str, AuthContext]):
        self.server = server
        self.tokens = tokens

    def auth(self, headers: dict[str, str]) -> AuthContext | None:
        auth_header = headers.get("Authorization") or headers.get("authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return None
        token = auth_header.split(" ", 1)[1].strip()
        return self.tokens.get(token)

    def require_auth(self, headers: dict[str, str]) -> AuthContext | None:
        if not self.tokens:
            return None
        return self.auth(headers)


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


def _tool_specs_for_context(server: ToolServer, ctx: AuthContext | None) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for name in server.list_tools():
        if ctx is not None and not can_call_tool(ctx.role, name):
            continue
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


def process_jsonrpc_message(
    app: McpHttpApp,
    message: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any] | None]:
    if not isinstance(message, dict):
        return HTTPStatus.BAD_REQUEST, _jsonrpc_error(None, -32600, "Invalid Request")

    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") if "params" in message else {}
    if not isinstance(params, dict):
        return HTTPStatus.BAD_REQUEST, _jsonrpc_error(message_id, -32602, "Invalid params")

    if message_id is None and isinstance(method, str) and method.startswith("notifications/"):
        return HTTPStatus.ACCEPTED, None

    request_headers = headers or {}
    ctx = app.require_auth(request_headers)
    if app.tokens and method in {"tools/list", "tools/call"} and ctx is None:
        return HTTPStatus.UNAUTHORIZED, _jsonrpc_error(message_id, -32001, "Unauthorized")

    if method == "initialize":
        protocol_version = params.get("protocolVersion")
        if not isinstance(protocol_version, str):
            protocol_version = DEFAULT_PROTOCOL_VERSION
        return HTTPStatus.OK, _jsonrpc_result(
            message_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    if method == "ping":
        return HTTPStatus.OK, _jsonrpc_result(message_id, {})

    if method == "tools/list":
        return HTTPStatus.OK, _jsonrpc_result(
            message_id,
            {"tools": _tool_specs_for_context(app.server, ctx)},
        )

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(tool_name, str):
            return HTTPStatus.BAD_REQUEST, _jsonrpc_error(message_id, -32602, "name must be a string")
        if not isinstance(arguments, dict):
            return HTTPStatus.BAD_REQUEST, _jsonrpc_error(message_id, -32602, "arguments must be an object")
        return HTTPStatus.OK, _jsonrpc_result(
            message_id,
            _tool_call_result(app.server, tool_name, arguments, auth_context=ctx),
        )

    if isinstance(method, str) and method.startswith("notifications/"):
        return HTTPStatus.ACCEPTED, None

    return HTTPStatus.NOT_FOUND, _jsonrpc_error(message_id, -32601, "Method not found")


def metrics_access_status(app: McpHttpApp, headers: dict[str, str] | None = None) -> int:
    request_headers = headers or {}
    ctx = app.require_auth(request_headers)
    if app.tokens and ctx is None:
        return HTTPStatus.UNAUTHORIZED
    if ctx is not None and not can_call_tool(ctx.role, "get_metrics"):
        return HTTPStatus.FORBIDDEN
    return HTTPStatus.OK


def make_handler(app: McpHttpApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ArchivistMCPHTTP/0.1"

        def do_GET(self) -> None:              
            if self.path == "/health":
                _json(self, HTTPStatus.OK, {"status": "ok", "server": SERVER_NAME, "version": SERVER_VERSION})
                return
            if self.path == "/metrics":
                status = metrics_access_status(app, dict(self.headers.items()))
                if status == HTTPStatus.UNAUTHORIZED:
                    _json(self, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if status == HTTPStatus.FORBIDDEN:
                    _json(self, HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                    return
                _text(
                    self,
                    HTTPStatus.OK,
                    app.server.metrics.render_prometheus().encode("utf-8"),
                    "text/plain; version=0.0.4; charset=utf-8",
                )
                return
            _json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:              
            if self.path != "/mcp":
                _json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                msg = json.loads(body)
            except json.JSONDecodeError:
                _json(self, HTTPStatus.BAD_REQUEST, _jsonrpc_error(None, -32700, "Parse error"))
                return
            except Exception:
                _json(self, HTTPStatus.BAD_REQUEST, _jsonrpc_error(None, -32600, "Invalid Request"))
                return

            if not isinstance(msg, dict):
                _json(self, HTTPStatus.BAD_REQUEST, _jsonrpc_error(None, -32600, "Invalid Request"))
                return
            status, payload = process_jsonrpc_message(app, msg, dict(self.headers.items()))
            if payload is None:
                self.send_response(status)
                self.end_headers()
                return
            _json(self, status, payload)

        def log_message(self, fmt: str, *args) -> None:
            return

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=".archivist/archivist.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--require-user-id", action="store_true")
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
        logger=setup_structured_logger("archivist.mcp_http")
        if config.observability.structured_logging
        else None,
        enable_experimental_tools=config.feature_flags.experimental_tools_enabled,
        disabled_tools=set(config.feature_flags.disabled_tools),
    )

    try:
        tokens = load_token_map()
    except ValueError as exc:
        raise SystemExit(f"invalid ARCHIVIST_SSE_TOKENS: {exc}") from exc
    app = McpHttpApp(server, tokens)
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
    print(f"MCP HTTP server listening on {scheme}://{args.host}:{args.port}/mcp")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        conn.close()


if __name__ == "__main__":
    main()
