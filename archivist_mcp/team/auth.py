"""Team mode authn/authz middleware primitives."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    role: str
    project_ids: tuple[str, ...]


ROLE_ORDER = {"reader": 0, "writer": 1, "maintainer": 2, "admin": 3}

TOOL_MIN_ROLE = {
    "health": "reader",
    "version": "reader",
    "get_capabilities": "reader",
    "get_metrics": "maintainer",
    "read_node": "reader",
    "search_graph": "reader",
    "get_project_summary": "reader",
    "list_recent_incidents": "reader",
    "create_entity": "writer",
    "update_entity": "writer",
    "create_edge": "writer",
    "store_observation": "writer",
    "archive_decision": "writer",
    "deprecate_node": "writer",
    "compact_core_memory": "writer",
    "extract_symbols": "maintainer",
    "rebuild_embeddings": "maintainer",
    "rebuild_index_and_embeddings": "maintainer",
    "export_audit_log": "maintainer",
    "purge_observations": "maintainer",
    "resolve_conflict": "writer",
    "promote_branch_record": "writer",
    "invalidate_stale_memory": "writer",
}


def can_call_tool(role: str, tool_name: str) -> bool:
    required = TOOL_MIN_ROLE.get(tool_name, "admin")
    return ROLE_ORDER.get(role, -1) >= ROLE_ORDER.get(required, 99)


def is_project_allowed(ctx: AuthContext, project_id: str) -> bool:
    return project_id in ctx.project_ids


def load_token_map() -> dict[str, AuthContext]:
    raw = os.getenv("ARCHIVIST_SSE_TOKENS", "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("ARCHIVIST_SSE_TOKENS is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("ARCHIVIST_SSE_TOKENS must be a JSON object of token -> context")
    out: dict[str, AuthContext] = {}
    for token, v in data.items():
        if not isinstance(token, str) or not token:
            raise ValueError("ARCHIVIST_SSE_TOKENS contains an invalid token key")
        if not isinstance(v, dict):
            raise ValueError(f"ARCHIVIST_SSE_TOKENS[{token}] must be an object")
        uid = v.get("user_id")
        role = v.get("role")
        projects = v.get("projects") or []
        if not isinstance(uid, str) or not isinstance(role, str):
            raise ValueError(f"ARCHIVIST_SSE_TOKENS[{token}] must include string user_id and role")
        if role not in ROLE_ORDER:
            raise ValueError(f"ARCHIVIST_SSE_TOKENS[{token}] has unsupported role: {role}")
        if not isinstance(projects, list) or not all(isinstance(p, str) for p in projects):
            raise ValueError(f"ARCHIVIST_SSE_TOKENS[{token}] projects must be a string array")
        out[token] = AuthContext(user_id=uid, role=role, project_ids=tuple(projects))
    if not out:
        raise ValueError("ARCHIVIST_SSE_TOKENS is set but no valid tokens were provided")
    return out
