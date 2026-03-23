"""Embedding provider abstraction and worker."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from typing import Protocol


class EmbeddingProvider(Protocol):
    name: str
    dimensions: int

    def embed_text(self, text: str) -> list[float]:
        ...


@dataclass
class HashEmbeddingProvider:
    """Deterministic offline local provider for development and testing."""

    name: str = "hash-local"
    dimensions: int = 384

    def embed_text(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        if not text:
            return vec
        for token in text.lower().split():
            h = int(hashlib.sha1(token.encode("utf-8")).hexdigest(), 16)
            idx = h % self.dimensions
            vec[idx] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


@dataclass
class EmbeddingConfig:
    enabled: bool = True
    provider: str = "hash-local"
    model: str = "bge-small-en-v1.5"
    dimensions: int = 384
    offline_strict: bool = True


class EmbeddingWorker:
    def __init__(self, conn: sqlite3.Connection, config: EmbeddingConfig):
        self.conn = conn
        self.config = config
        self.provider = self._build_provider(config)

    def available(self) -> bool:
        return self.config.enabled and self.provider is not None

    def embed_query(self, text: str) -> list[float] | None:
        if not self.available():
            return None
        return self.provider.embed_text(text)

    def upsert_node_embedding(self, node_id: str, text: str) -> None:
        if not self.available():
            return
        vec = self.provider.embed_text(text)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO node_embeddings(node_id, model, dimensions, vector_json, updated_at)
                VALUES (?, ?, ?, ?, (strftime('%Y-%m-%dT%H:%M:%fZ','now')))
                ON CONFLICT(node_id)
                DO UPDATE SET
                    model = excluded.model,
                    dimensions = excluded.dimensions,
                    vector_json = excluded.vector_json,
                    updated_at = excluded.updated_at
                """,
                (node_id, self.config.model, self.provider.dimensions, json.dumps(vec)),
            )

    def upsert_observation_embedding(self, observation_id: str, text: str) -> None:
        if not self.available():
            return
        vec = self.provider.embed_text(text)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO observation_embeddings(observation_id, model, dimensions, vector_json, updated_at)
                VALUES (?, ?, ?, ?, (strftime('%Y-%m-%dT%H:%M:%fZ','now')))
                ON CONFLICT(observation_id)
                DO UPDATE SET
                    model = excluded.model,
                    dimensions = excluded.dimensions,
                    vector_json = excluded.vector_json,
                    updated_at = excluded.updated_at
                """,
                (observation_id, self.config.model, self.provider.dimensions, json.dumps(vec)),
            )

    def rebuild_node_embeddings(self, project_id: str) -> int:
        if not self.available():
            return 0
        rows = self.conn.execute(
            "SELECT node_id, title, content FROM nodes WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        for row in rows:
            self.upsert_node_embedding(row["node_id"], f"{row['title']}\n{row['content']}")
        return len(rows)

    def _build_provider(self, config: EmbeddingConfig) -> EmbeddingProvider | None:
        if not config.enabled:
            return None
                                                      
        if config.offline_strict and config.provider not in {"hash-local", "ollama", "llama-cpp", "transformers-local"}:
            return None
        if config.provider == "hash-local":
            return HashEmbeddingProvider(dimensions=config.dimensions)
                                                                               
        return None
