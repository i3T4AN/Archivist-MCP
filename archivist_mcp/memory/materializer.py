"""Core memory materialization for markdown and JSON outputs."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any


class CoreMemoryMaterializer:
    """Builds compact core_memory outputs from archival state."""

    def __init__(self, conn: sqlite3.Connection, output_dir: Path, core_max_kb: int = 12):
        self.conn = conn
        self.output_dir = output_dir
        self.max_bytes = core_max_kb * 1024

    def refresh(self, project_id: str) -> dict[str, Any]:
        decisions = self._fetch_nodes(project_id, "Decision")
        rules = self._fetch_nodes(project_id, "Rule")
        incidents = self._fetch_incidents(project_id)
        architecture = self._fetch_architecture(project_id)

        payload = {
            "project_id": project_id,
            "sections": {
                "decisions": decisions,
                "rules": rules,
                "high_priority_incidents": incidents,
                "architecture_map": architecture,
            },
            "metadata": {
                "budget_bytes": self.max_bytes,
                "truncated": False,
                "included_items": 0,
                "excluded_items": 0,
            },
        }

        payload = self._apply_budget(payload)
        markdown = self._to_markdown(payload)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self.output_dir / "core_memory.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
        self._atomic_write(self.output_dir / "core_memory.md", markdown)
        return payload

    def _apply_budget(self, payload: dict[str, Any]) -> dict[str, Any]:
        candidates: list[tuple[str, dict[str, Any]]] = []
        for section in ("decisions", "rules", "high_priority_incidents", "architecture_map"):
            for item in payload["sections"][section]:
                candidates.append((section, item))

        selected: dict[str, list[dict[str, Any]]] = {
            "decisions": [],
            "rules": [],
            "high_priority_incidents": [],
            "architecture_map": [],
        }

                                                                                           
        section_order = {"decisions": 0, "rules": 1, "high_priority_incidents": 2, "architecture_map": 3}
        candidates.sort(key=lambda x: x[1].get("node_id", x[1].get("entity", "~")))
        candidates.sort(key=lambda x: x[1].get("sort_updated_at", ""), reverse=True)
        candidates.sort(key=lambda x: section_order[x[0]])

        included = 0
        excluded = 0

        for section, item in candidates:
            selected[section].append(item)
            draft = {
                "project_id": payload["project_id"],
                "sections": selected,
                "metadata": payload["metadata"],
            }
            size = len((json.dumps(draft, sort_keys=True) + "\n").encode("utf-8"))
            if size <= self.max_bytes:
                included += 1
                continue
            selected[section].pop()
            excluded += 1

        payload["sections"] = selected
        payload["metadata"]["included_items"] = included
        payload["metadata"]["excluded_items"] = excluded
        payload["metadata"]["truncated"] = excluded > 0
        return payload

    def _fetch_nodes(self, project_id: str, node_type: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT node_id, title, content, state, updated_at
            FROM nodes
            WHERE project_id = ? AND type = ? AND state = 'active'
            ORDER BY updated_at DESC, node_id ASC
            """,
            (project_id, node_type),
        ).fetchall()
        return [
            {
                "node_id": row["node_id"],
                "title": row["title"],
                "content": row["content"],
                "state": row["state"],
                "sort_updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def _fetch_incidents(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT n.node_id, n.title, n.content, n.state, n.updated_at,
                   np.value_json AS severity_json
            FROM nodes n
            LEFT JOIN node_properties np ON np.node_id = n.node_id AND np.key = 'incident_severity'
            WHERE n.project_id = ? AND n.type = 'Incident' AND n.state = 'active'
            ORDER BY n.updated_at DESC, n.node_id ASC
            """,
            (project_id,),
        ).fetchall()

        incidents: list[dict[str, Any]] = []
        for row in rows:
            sev = ""
            if row["severity_json"]:
                try:
                    sev = str(json.loads(row["severity_json"])).lower()
                except json.JSONDecodeError:
                    sev = ""
            if sev in ("sev1", "sev2", "high", "critical") or not sev:
                incidents.append(
                    {
                        "node_id": row["node_id"],
                        "title": row["title"],
                        "content": row["content"],
                        "state": row["state"],
                        "severity": sev or "unknown",
                        "sort_updated_at": row["updated_at"],
                    }
                )
        return incidents

    def _fetch_architecture(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT e.node_id, e.title, e.updated_at,
                   COUNT(ed.edge_id) AS outgoing_depends_on
            FROM nodes e
            LEFT JOIN edges ed
              ON ed.project_id = e.project_id
             AND ed.from_node_id = e.node_id
             AND ed.type = 'DEPENDS_ON'
             AND ed.state = 'active'
            WHERE e.project_id = ? AND e.type = 'Entity' AND e.state = 'active'
            GROUP BY e.node_id, e.title, e.updated_at
            ORDER BY outgoing_depends_on DESC, e.updated_at DESC, e.node_id ASC
            LIMIT 25
            """,
            (project_id,),
        ).fetchall()
        return [
            {
                "entity": row["title"],
                "node_id": row["node_id"],
                "outgoing_depends_on": row["outgoing_depends_on"],
                "sort_updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def _to_markdown(self, payload: dict[str, Any]) -> str:
        s = payload["sections"]
        lines = [
            "# Core Memory",
            "",
            f"Project: {payload['project_id']}",
            f"Truncated: {str(payload['metadata']['truncated']).lower()}",
            "",
            "## Active Decisions",
        ]
        lines.extend(self._bullets(s["decisions"], lambda x: f"{x['title']} ({x['node_id']})"))
        lines.extend(["", "## Active Rules"])
        lines.extend(self._bullets(s["rules"], lambda x: f"{x['title']} ({x['node_id']})"))
        lines.extend(["", "## High Priority/Open Incidents"])
        lines.extend(self._bullets(s["high_priority_incidents"], lambda x: f"{x['title']} [{x['severity']}]"))
        lines.extend(["", "## Architecture Map Summary"])
        lines.extend(self._bullets(s["architecture_map"], lambda x: f"{x['entity']} deps:{x['outgoing_depends_on']}"))
        lines.append("")
        return "\n".join(lines)

    def _bullets(self, items: list[dict[str, Any]], fmt) -> list[str]:
        if not items:
            return ["- none"]
        return [f"- {fmt(item)}" for item in items]

    def _atomic_write(self, path: Path, content: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
