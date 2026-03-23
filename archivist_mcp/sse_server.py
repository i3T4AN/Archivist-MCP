"""SSE transport for team mode with authn/authz enforcement."""

from __future__ import annotations

import argparse
import json
import queue
import ssl
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from archivist_mcp.config import load_config
from archivist_mcp.db import connect
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


class EventBus:
    def __init__(self) -> None:
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs = [s for s in self._subs if s is not q]

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                continue


class SseApp:
    def __init__(self, server: ToolServer, tokens: dict[str, AuthContext], event_bus: EventBus):
        self.server = server
        self.tokens = tokens
        self.event_bus = event_bus

    def auth(self, headers: dict[str, str]) -> AuthContext | None:
        auth_header = headers.get("Authorization") or headers.get("authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return None
        token = auth_header.split(" ", 1)[1].strip()
        return self.tokens.get(token)

    def metrics_error(self, headers: dict[str, str]) -> str | None:
        if not self.tokens:
            return None
        ctx = self.auth(headers)
        if ctx is None:
            return "unauthorized"
        if not can_call_tool(ctx.role, "get_metrics"):
            return "forbidden"
        return None


def _status_for_tool_response(result: dict[str, Any]) -> int:
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


def make_handler(app: SseApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ArchivistSSE/0.1"

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:              
            if urlparse(self.path).path != "/tool":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            ctx = app.auth(dict(self.headers.items()))
            if ctx is None:
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
                return

            tool = payload.get("tool")
            args = payload.get("args") or {}
            trace_id = payload.get("trace_id")
            result = app.server.handle_tool(tool, args, trace_id=trace_id, auth_context=ctx)
            self._json(_status_for_tool_response(result), result)

        def do_GET(self) -> None:              
            path = urlparse(self.path).path
            if path == "/metrics":
                err = app.metrics_error(dict(self.headers.items()))
                if err == "unauthorized":
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if err == "forbidden":
                    self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                    return
                data = app.server.metrics.render_prometheus().encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            if path != "/events":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            ctx = app.auth(dict(self.headers.items()))
            if ctx is None:
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return

            q = app.event_bus.subscribe()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            try:
                               
                hello = {"event": "connected", "user_id": ctx.user_id, "role": ctx.role, "projects": list(ctx.project_ids)}
                self.wfile.write(f"event: connected\ndata: {json.dumps(hello)}\n\n".encode("utf-8"))
                self.wfile.flush()

                while True:
                    try:
                        event = q.get(timeout=10)
                        project_id = event.get("project_id")
                        if isinstance(project_id, str) and project_id not in ctx.project_ids:
                            continue
                        self.wfile.write(f"event: {event.get('event','message')}\ndata: {json.dumps(event)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except BrokenPipeError:
                pass
            finally:
                app.event_bus.unsubscribe(q)

        def log_message(self, fmt: str, *args) -> None:
            return

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=".archivist/archivist.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
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

    core_dir = Path(args.db).resolve().parent
    materializer = CoreMemoryMaterializer(conn, output_dir=core_dir, core_max_kb=config.memory.core_max_kb)
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

    event_bus = EventBus()
    server = ToolServer(
        conn,
        require_user_id=True,
        core_materializer=materializer,
        embedding_worker=embedding_worker,
        retrieval_engine=retrieval_engine,
        event_emitter=event_bus.publish,
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
        logger=setup_structured_logger("archivist.sse")
        if config.observability.structured_logging
        else None,
        enable_experimental_tools=config.feature_flags.experimental_tools_enabled,
        disabled_tools=set(config.feature_flags.disabled_tools),
    )

    try:
        tokens = load_token_map()
    except ValueError as exc:
        raise SystemExit(f"invalid ARCHIVIST_SSE_TOKENS: {exc}") from exc
    app = SseApp(server, tokens, event_bus)
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
    print(f"SSE server listening on {scheme}://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        conn.close()


if __name__ == "__main__":
    main()
