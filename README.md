# Archivist MCP

*Local-first project memory server for agents and developers*

[![Python](https://img.shields.io/badge/Python-3.11+-blue?style=flat-square)](https://www.python.org)
[![SQLite](https://img.shields.io/badge/SQLite-WAL-003B57?style=flat-square)](https://www.sqlite.org)
[![MCP](https://img.shields.io/badge/MCP-JSON--RPC-0A7E8C?style=flat-square)](https://modelcontextprotocol.io)

[Features](#features) • [Quick Start](#quick-start) • [MCP Setup](#mcp-setup) • [WebUI](#webui) • [Usage](#usage) • [Tools](#mcp-tools) • [Configuration](#configuration) • [Reliability](#reliability--ops) • [Troubleshooting](#troubleshooting)

---

**Archivist MCP** is a memory layer for coding agents. You run it alongside your project, connect your MCP client, and the agent can persist/retrieve decisions, incidents, rules, and architecture context across sessions. WebUI is for human review and controlled write actions when you want visibility before changing memory.

## Features

- **Three-tier memory model**: working memory, compact core memory (`core_memory.md/.json`), archival graph
- **Graph + lifecycle safety**: typed nodes/edges, state transitions, optimistic concurrency, immutable audit events
- **Hybrid retrieval**: FTS + local embeddings + graph degree + recency with provenance and confidence
- **MCP transports**: JSON-RPC over STDIO and HTTP (`/mcp`)
- **Team mode**: SSE transport with bearer-token auth, role matrix, and project scoping
- **Controlled write workflows**: conflict resolution, branch-to-project promotion, stale-memory invalidation
- **Security hardening**: payload allowlists, size/type validation, sanitization, redaction, retention purge
- **Reliability tooling**: integrity checks, snapshots, restore, startup recovery, rebuild index+embeddings

## Quick Start

### Prerequisites

- Python `3.11+`
- macOS/Linux/Windows

### 1) Initialize database

```bash
python3 scripts/migrate.py --db .archivist/archivist.db
```

### 2) Start STDIO server

```bash
python3 -m archivist_mcp.stdio_server --db .archivist/archivist.db
```

### 3) Seed a project/user (first-time)

```bash
python3 - <<'PY'
from archivist_mcp.db import connect
conn = connect('.archivist/archivist.db')
conn.execute("INSERT OR IGNORE INTO projects(project_id,name) VALUES('proj-1','Project One')")
conn.execute("INSERT OR IGNORE INTO users(user_id,display_name) VALUES('user-1','User One')")
conn.commit()
conn.close()
PY
```

## MCP Setup

### Option A: VS Code / Codex Extension (STDIO MCP server)

In MCP settings (`Add MCP Server`) use:

- **Name**: `archivist-mcp`
- **Command to launch**: `python3`
- **Arguments**: `-m archivist_mcp.mcp_stdio_server --db .archivist/archivist.db`
- **Working directory**: your repo root (example: `/path/to/your/repo`)
- **Env vars**: optional (`ARCHIVIST_CONFIG_PATH`, etc.)

If you need strict team-style user enforcement in stdio mode, add `--require-user-id`.

### Option B: MCP over HTTP bridge

If your MCP client expects stdio process launch but you want the HTTP server, launch a bridge command such as:

```bash
npx -y mcp-remote http://127.0.0.1:8766/mcp
```

## WebUI

Start directly:

```bash
python3 -m archivist_mcp.mcp_http_server --db .archivist/archivist.db --host 127.0.0.1 --port 8766
python3 -m archivist_mcp.sse_server --db .archivist/archivist.db --host 127.0.0.1 --port 8765
python3 -m archivist_mcp.webui_server --db .archivist/archivist.db --host 127.0.0.1 --port 8090
```

Open:

- `http://127.0.0.1:8090`

Views:

- Search (with explain-why provenance)
- Graph
- Decision timeline
- Incident timeline
- Conflict inbox
- Controls (rule writes, conflict resolve, scope promotion, memory invalidation)

## Usage

### What this project is for

Use case:
1. You work with an agent on a codebase over many sessions.
2. Important context normally gets lost between sessions.
3. Archivist stores that context in a local graph and makes it available through MCP tools.
4. The agent can then recall prior decisions/conflicts/incidents instead of re-discovering them.

### How tool discovery and invocation works

1. After MCP connection, the client initializes the server and requests available tools (`tools/list`).
2. The client chooses a tool based on your prompt and invokes it (`tools/call`) with arguments.
3. You usually do not call MCP tools manually; your agent does it for you.
4. If your client has a tools panel, you can verify connection by checking the listed Archivist tools.

### When to use which tool family

- **Capture memory**: `create_entity`, `archive_decision`, `store_observation`, `create_edge`
- **Recall context**: `search_graph`, `read_node`, `get_project_summary`, `list_recent_incidents`
- **Maintain memory quality**: `update_entity`, `deprecate_node`, `resolve_conflict`, `invalidate_stale_memory`
- **Codebase-derived memory**: `extract_symbols`, `rebuild_index_and_embeddings`
- **Ops/compliance**: `export_audit_log`, `purge_observations`, `get_metrics`

### Typical day-to-day flow

1. Agent reads context with `search_graph`.
2. During work, agent writes new facts/decisions.
3. If stale data appears, agent or human resolves/deprecates it.
4. You review timelines/conflicts in WebUI when needed.

### What gets persisted

- Nodes: decisions, incidents, rules, entities, observations
- Edges: relationships like dependencies, resolution links, deprecations
- Audit/conflict records
- Compact core summary files: `core_memory.md` and `core_memory.json`

## MCP Tools

Current tool set (subject to feature flags):

- `health`
- `version`
- `get_capabilities`
- `get_metrics`
- `create_entity`
- `read_node`
- `update_entity`
- `create_edge`
- `search_graph`
- `store_observation`
- `archive_decision`
- `get_project_summary`
- `list_recent_incidents`
- `deprecate_node`
- `compact_core_memory`
- `extract_symbols`
- `rebuild_embeddings`
- `rebuild_index_and_embeddings`
- `export_audit_log`
- `purge_observations`
- `resolve_conflict`
- `promote_branch_record`
- `invalidate_stale_memory`

`search_graph` note:
- `include_deprecated=false` returns active records only.
- `include_deprecated=true` allows deprecated/invalidated/superseded records (archived excluded).

## Configuration

Config source order:

1. `.archivist/config.toml` (or `ARCHIVIST_CONFIG_PATH`)
2. environment variable overrides

Common env vars:

- `ARCHIVIST_CONFIG_PATH`
- `ARCHIVIST_DISABLE_EMBEDDINGS=true|false`
- `ARCHIVIST_CORE_MAX_KB`
- `ARCHIVIST_RATE_LIMIT_PER_MINUTE`
- `ARCHIVIST_STRUCTURED_LOGGING=false`
- `ARCHIVIST_DB_ENCRYPTION_KEY`
- `ARCHIVIST_ENCRYPTION_REQUIRED=true`
- `ARCHIVIST_SSE_TOKENS` (JSON token map for team auth)
- `ARCHIVIST_TLS_ENABLED=true`
- `ARCHIVIST_TLS_CERT_FILE`, `ARCHIVIST_TLS_KEY_FILE`

Example team token map:

```json
{
  "token-a": {
    "user_id": "user-1",
    "role": "writer",
    "projects": ["proj-1"]
  },
  "token-b": {
    "user_id": "maint-1",
    "role": "maintainer",
    "projects": ["proj-1"]
  }
}
```

Encryption behavior:
- If `ARCHIVIST_DB_ENCRYPTION_KEY` is set and SQLCipher is unavailable, startup fails.
- `ARCHIVIST_ENCRYPTION_REQUIRED=true` also enforces fail-closed encryption checks.

## Reliability & Ops

### Integrity check

```bash
python3 scripts/check_integrity.py --db .archivist/archivist.db
```

### Snapshot and restore

```bash
python3 scripts/create_snapshot.py --db .archivist/archivist.db --snapshot-dir .archivist/snapshots
python3 scripts/restore_snapshot.py --snapshot .archivist/snapshots/<snapshot>.db --db .archivist/archivist.db
```

### Rebuild derived state

```bash
python3 scripts/rebuild_index_and_embeddings.py --db .archivist/archivist.db --project-id proj-1 --root .
```

## Project Structure

```text
archivist_mcp/
  mcp_stdio_server.py      # MCP JSON-RPC over stdio
  mcp_http_server.py       # MCP JSON-RPC over HTTP (/mcp)
  sse_server.py            # Team mode HTTP+SSE
  webui_server.py          # Browser UI + controlled write APIs
  tooling/server.py        # Tool router, validation, envelope/errors
  storage/repository.py    # Graph persistence + lifecycle + audit
  retrieval/               # embeddings + hybrid retrieval
  indexing/                # symbol extraction + incremental indexer
  memory/materializer.py   # core_memory.md + core_memory.json
  migrations/sql/          # schema migrations
scripts/
  migrate.py
  create_snapshot.py
  restore_snapshot.py
  check_integrity.py
  rebuild_index_and_embeddings.py
tests/
  test suites for integration, retrieval, security, team mode, and WebUI behavior
docs/
  quickstart.md
  troubleshooting.md
  recovery_runbook.md
```

## Troubleshooting

### `AUTHZ_DENIED`

Token role/scope does not permit the tool or project. Check `ARCHIVIST_SSE_TOKENS` role and `projects`.

### `CONFLICT_ERROR`

Optimistic version mismatch. Re-read node, use latest `version`, retry or resolve via conflict workflow.

### `EMBEDDING_DISABLED`

Embeddings are disabled/unavailable. Retrieval falls back to `fts_graph` mode.

### Blank WebUI timelines

The UI reads from the DB passed to `webui_server --db`. Ensure your seeding and servers all point at the same DB path.

## Documentation

- Quickstart: [docs/quickstart.md](docs/quickstart.md)
- Troubleshooting: [docs/troubleshooting.md](docs/troubleshooting.md)
- Upgrade/Migration: [docs/upgrade_migration.md](docs/upgrade_migration.md)
- Recovery runbook: [docs/recovery_runbook.md](docs/recovery_runbook.md)
- Security threat model: [docs/security_threat_model.md](docs/security_threat_model.md)

---

Built for durable project memory across agent sessions, with local-first defaults and auditable writes.
