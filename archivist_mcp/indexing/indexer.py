"""Incremental code graph indexing service."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from archivist_mcp.indexing.symbol_extractor import ExtractionResult, detect_language, extract_symbols_for_file
from archivist_mcp.storage.repository import GraphRepository
from archivist_mcp.errors import ConstraintError


@dataclass
class IndexReport:
    project_id: str
    scanned_files: int
    changed_files: int
    symbols_added_or_updated: int
    symbols_deprecated: int
    dependencies_created: int
    duration_ms: int


class SymbolIndexer:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.repo = GraphRepository(conn)

    def index_project(self, project_id: str, root: Path, incremental: bool = True) -> IndexReport:
        start = time.perf_counter()
        files = self._collect_supported_files(root)
        changed = []

        for path in files:
            h = self._hash_file(path)
            mtime = path.stat().st_mtime_ns
            prev = self.conn.execute(
                "SELECT content_hash, mtime_ns FROM code_index_files WHERE project_id = ? AND file_path = ?",
                (project_id, str(path)),
            ).fetchone()
            if not incremental or prev is None or prev["content_hash"] != h or prev["mtime_ns"] != mtime:
                changed.append(path)

        total_symbols = 0
        total_deprecated = 0
        total_dependencies = 0

        total_deprecated += self._deprecate_removed_files(project_id, files)

        for path in changed:
            text = path.read_text(encoding="utf-8", errors="ignore")
            result = extract_symbols_for_file(path, text, project_id)
            added, deprecated, dependencies = self._apply_file_delta(project_id, path, result)
            total_symbols += added
            total_deprecated += deprecated
            total_dependencies += dependencies
            self._upsert_file_state(project_id, path)

        duration_ms = int((time.perf_counter() - start) * 1000)
        return IndexReport(
            project_id=project_id,
            scanned_files=len(files),
            changed_files=len(changed),
            symbols_added_or_updated=total_symbols,
            symbols_deprecated=total_deprecated,
            dependencies_created=total_dependencies,
            duration_ms=duration_ms,
        )

    def _deprecate_removed_files(self, project_id: str, current_files: list[Path]) -> int:
        current_set = {str(p) for p in current_files}
        rows = self.conn.execute(
            "SELECT file_path FROM code_index_files WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        removed = [row["file_path"] for row in rows if row["file_path"] not in current_set]
        if not removed:
            return 0

        deprecated = 0
        for file_path in removed:
            symbol_rows = self.conn.execute(
                """
                SELECT n.node_id, n.version, n.state
                FROM nodes n
                JOIN node_properties p ON p.node_id = n.node_id
                WHERE n.project_id = ?
                  AND n.type = 'Entity'
                  AND p.key = 'symbol_file_path'
                  AND json_extract(p.value_json, '$') = ?
                """,
                (project_id, file_path),
            ).fetchall()
            for row in symbol_rows:
                if row["state"] != "active":
                    continue
                self.repo.update_node(
                    node_id=row["node_id"],
                    expected_version=row["version"],
                    actor_id=None,
                    state="deprecated",
                )
                deprecated += 1
            with self.conn:
                self.conn.execute(
                    "DELETE FROM code_index_files WHERE project_id = ? AND file_path = ?",
                    (project_id, file_path),
                )
        return deprecated

    def _apply_file_delta(self, project_id: str, path: Path, result: ExtractionResult) -> tuple[int, int, int]:
        file_path = str(path)
        existing_rows = self.conn.execute(
            """
            SELECT n.node_id, n.version, n.state
            FROM nodes n
            JOIN node_properties p ON p.node_id = n.node_id
            WHERE n.project_id = ?
              AND n.type = 'Entity'
              AND p.key = 'symbol_file_path'
              AND json_extract(p.value_json, '$') = ?
            """,
            (project_id, file_path),
        ).fetchall()

        existing_ids = {row["node_id"]: row for row in existing_rows}
        extracted_ids = {s.stable_id for s in result.symbols}

        added_or_updated = 0
        for symbol in result.symbols:
            row = existing_ids.get(symbol.stable_id)
            if row is None:
                self.repo.create_node(
                    node_id=symbol.stable_id,
                    project_id=project_id,
                    node_type="Entity",
                    title=symbol.name,
                    content=symbol.signature,
                    actor_id=None,
                    state="active",
                )
                added_or_updated += 1
            else:
                                                                            
                self.repo.update_node(
                    node_id=symbol.stable_id,
                    expected_version=row["version"],
                    actor_id=None,
                    title=symbol.name,
                    content=symbol.signature,
                    state="active" if row["state"] == "active" else "deprecated",
                )
                added_or_updated += 1

            self.repo.upsert_node_property(symbol.stable_id, "symbol_file_path", file_path)
            self.repo.upsert_node_property(symbol.stable_id, "symbol_kind", symbol.kind)
            self.repo.upsert_node_property(symbol.stable_id, "symbol_language", symbol.language)
            self.repo.upsert_node_property(symbol.stable_id, "symbol_backend", symbol.backend)
            self.repo.upsert_node_property(symbol.stable_id, "symbol_range", {
                "start_line": symbol.start_line,
                "end_line": symbol.end_line,
            })

        deprecated = 0
        for existing_id, row in existing_ids.items():
            if existing_id not in extracted_ids and row["state"] == "active":
                self.repo.update_node(
                    node_id=existing_id,
                    expected_version=row["version"],
                    actor_id=None,
                    state="deprecated",
                )
                deprecated += 1

        dependencies = self._persist_dependencies(project_id, result)
        return added_or_updated, deprecated, dependencies

    def _persist_dependencies(self, project_id: str, result: ExtractionResult) -> int:
        dep_count = 0
        all_symbols = [s.stable_id for s in result.symbols]
        external_targets = sorted(set(result.imports + result.calls))
        for dep_name in external_targets:
            dep_id = self._external_dep_node_id(project_id, dep_name)
            row = self.conn.execute(
                "SELECT node_id, version FROM nodes WHERE node_id = ?",
                (dep_id,),
            ).fetchone()
            if row is None:
                self.repo.create_node(
                    node_id=dep_id,
                    project_id=project_id,
                    node_type="Entity",
                    title=dep_name,
                    content=f"external dependency: {dep_name}",
                    actor_id=None,
                )
            for sid in all_symbols:
                try:
                    self.repo.create_edge(
                        project_id=project_id,
                        edge_type="DEPENDS_ON",
                        from_node_id=sid,
                        to_node_id=dep_id,
                        actor_id=None,
                    )
                    dep_count += 1
                except ConstraintError:
                                                                             
                    continue
        return dep_count

    def _collect_supported_files(self, root: Path) -> list[Path]:
        out: list[Path] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if ".git" in path.parts or "node_modules" in path.parts or ".archivist" in path.parts:
                continue
            if detect_language(path) is not None:
                out.append(path)
        out.sort(key=lambda p: str(p))
        return out

    def _hash_file(self, path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            while True:
                chunk = f.read(1024 * 64)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def _upsert_file_state(self, project_id: str, path: Path) -> None:
        mtime = path.stat().st_mtime_ns
        digest = self._hash_file(path)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO code_index_files(project_id, file_path, mtime_ns, content_hash)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id, file_path)
                DO UPDATE SET
                    mtime_ns = excluded.mtime_ns,
                    content_hash = excluded.content_hash,
                    updated_at = (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                """,
                (project_id, str(path), mtime, digest),
            )

    def _external_dep_node_id(self, project_id: str, dep_name: str) -> str:
        seed = f"{project_id}|dep|{dep_name}".encode("utf-8")
        return "dep_" + hashlib.sha1(seed).hexdigest()[:24]
