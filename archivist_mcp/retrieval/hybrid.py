"""Hybrid retrieval engine: FTS + vector + graph + recency."""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from archivist_mcp.retrieval.embeddings import EmbeddingWorker


@dataclass(frozen=True)
class RetrievalWeights:
    fts_weight: float = 0.35
    vector_weight: float = 0.35
    graph_weight: float = 0.20
    recency_weight: float = 0.10


class HybridRetrievalEngine:
    def __init__(self, conn: sqlite3.Connection, embedder: EmbeddingWorker, weights: RetrievalWeights):
        self.conn = conn
        self.embedder = embedder
        self.weights = weights

    def search(
        self,
        *,
        project_id: str,
        query: str,
        limit: int = 8,
        include_deprecated: bool = False,
    ) -> dict[str, Any]:
        warnings: list[str] = []
        fts_candidates = self._fts_candidates(project_id, query, max(limit * 8, 32), include_deprecated)
        if not fts_candidates:
            return {"results": [], "mode": "hybrid", "warnings": warnings}

        query_vec = self.embedder.embed_query(query)
        vector_enabled = query_vec is not None
        if not vector_enabled:
            warnings.append("EMBEDDING_DISABLED")

                                                 
        ids = [c["node_id"] for c in fts_candidates]
        vector_scores = self._vector_scores(ids, query_vec) if vector_enabled else {}
        graph_scores = self._graph_scores(project_id, ids)
        recency_scores = self._recency_scores(fts_candidates)

                                                       
        bm_values = [float(c["bm25_score"]) for c in fts_candidates]
        bm_min = min(bm_values)
        bm_max = max(bm_values)
        bm_range = (bm_max - bm_min) or 1.0

        results: list[dict[str, Any]] = []
        for c in fts_candidates:
            node_id = c["node_id"]
            fts_raw = float(c["bm25_score"])
            fts_score = 1.0 - ((fts_raw - bm_min) / bm_range)
            vector_score = vector_scores.get(node_id, 0.0)
            graph_score = graph_scores.get(node_id, 0.0)
            recency_score = recency_scores.get(node_id, 0.0)

            score = (
                self.weights.fts_weight * fts_score
                + self.weights.vector_weight * vector_score
                + self.weights.graph_weight * graph_score
                + self.weights.recency_weight * recency_score
            )

            state = c["state"]
            if state in {"deprecated", "invalidated"} and not include_deprecated:
                score *= 0.4

            confidence = max(0.0, min(1.0, score))
            results.append(
                {
                    "node_id": node_id,
                    "project_id": c["project_id"],
                    "type": c["type"],
                    "title": c["title"],
                    "content": c["content"],
                    "state": state,
                    "version": c["version"],
                    "score": round(score, 6),
                    "confidence": round(confidence, 6),
                    "provenance": {
                        "fts": round(fts_score, 6),
                        "vector": round(vector_score, 6),
                        "graph": round(graph_score, 6),
                        "recency": round(recency_score, 6),
                    },
                }
            )

        results.sort(key=lambda r: r["score"], reverse=True)
        return {
            "results": results[:limit],
            "mode": "hybrid" if vector_enabled else "fts_graph",
            "warnings": warnings,
        }

    def _fts_candidates(
        self,
        project_id: str,
        query: str,
        limit: int,
        include_deprecated: bool,
    ) -> list[dict[str, Any]]:
        sql = (
            """
            SELECT n.node_id, n.project_id, n.type, n.title, n.content, n.state, n.version,
                   n.updated_at, bm25(nodes_fts) AS bm25_score
            FROM nodes_fts
            JOIN nodes n ON n.node_id = nodes_fts.node_id
            WHERE n.project_id = ? AND nodes_fts MATCH ?
            """
        )
        params: list[Any] = [project_id, query]
        if not include_deprecated:
            sql += " AND n.state = 'active'"
        else:
            sql += " AND n.state != 'archived'"
        sql += " ORDER BY bm25_score ASC, n.updated_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def _vector_scores(self, node_ids: list[str], query_vec: list[float] | None) -> dict[str, float]:
        if query_vec is None or not node_ids:
            return {}
        ph = ",".join("?" for _ in node_ids)
        rows = self.conn.execute(
            f"SELECT node_id, vector_json FROM node_embeddings WHERE node_id IN ({ph})",
            tuple(node_ids),
        ).fetchall()

        scores: dict[str, float] = {}
        for row in rows:
            try:
                vec = json.loads(row["vector_json"])
            except json.JSONDecodeError:
                continue
            scores[row["node_id"]] = self._cosine(query_vec, vec)

                               
        if scores:
            mn = min(scores.values())
            mx = max(scores.values())
            rng = (mx - mn) or 1.0
            for k in list(scores.keys()):
                scores[k] = (scores[k] - mn) / rng
        return scores

    def _graph_scores(self, project_id: str, node_ids: list[str]) -> dict[str, float]:
        if not node_ids:
            return {}
        ph = ",".join("?" for _ in node_ids)
        rows = self.conn.execute(
            f"""
            SELECT n.node_id,
                   (
                       SELECT COUNT(*) FROM edges e1
                       WHERE e1.project_id = ? AND e1.state = 'active' AND e1.from_node_id = n.node_id
                   ) + (
                       SELECT COUNT(*) FROM edges e2
                       WHERE e2.project_id = ? AND e2.state = 'active' AND e2.to_node_id = n.node_id
                   ) AS degree
            FROM nodes n
            WHERE n.node_id IN ({ph})
            """,
            (project_id, project_id, *node_ids),
        ).fetchall()
        raw = {row["node_id"]: float(row["degree"]) for row in rows}
        if not raw:
            return {}
        mn = min(raw.values())
        mx = max(raw.values())
        rng = (mx - mn) or 1.0
        return {k: (v - mn) / rng for k, v in raw.items()}

    def _recency_scores(self, candidates: list[dict[str, Any]]) -> dict[str, float]:
        now = datetime.now(timezone.utc)
        ages: dict[str, float] = {}
        for c in candidates:
            ts = c.get("updated_at") or c.get("created_at")
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except Exception:
                dt = now
            age_days = max(0.0, (now - dt).total_seconds() / 86400.0)
            ages[c["node_id"]] = age_days
        if not ages:
            return {}
        mn = min(ages.values())
        mx = max(ages.values())
        rng = (mx - mn) or 1.0
                                      
        return {k: 1.0 - ((v - mn) / rng) for k, v in ages.items()}

    def _cosine(self, a: list[float], b: list[float]) -> float:
        n = min(len(a), len(b))
        if n == 0:
            return 0.0
        dot = sum(a[i] * b[i] for i in range(n))
        na = math.sqrt(sum(a[i] * a[i] for i in range(n))) or 1.0
        nb = math.sqrt(sum(b[i] * b[i] for i in range(n))) or 1.0
        return dot / (na * nb)
