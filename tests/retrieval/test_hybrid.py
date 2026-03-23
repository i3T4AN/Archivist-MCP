from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from archivist_mcp.db import connect
from archivist_mcp.migrations.runner import run_migrations
from archivist_mcp.retrieval.embeddings import EmbeddingConfig, EmbeddingWorker
from archivist_mcp.retrieval.hybrid import HybridRetrievalEngine, RetrievalWeights


class HybridRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = Path(self.tmpdir.name) / "retrieval.db"
        self.conn = connect(str(self.db))
        run_migrations(self.conn, Path("archivist_mcp/migrations/sql"))
        self.conn.execute("INSERT INTO projects(project_id, name) VALUES ('p1', 'Project')")
        self.conn.execute(
            "INSERT INTO nodes(node_id, project_id, type, title, content, state) VALUES ('n1','p1','Entity','Parser','parses code tokens','active')"
        )
        self.conn.execute(
            "INSERT INTO nodes(node_id, project_id, type, title, content, state) VALUES ('n2','p1','Entity','LegacyParser','old deprecated parser','deprecated')"
        )
        self.conn.execute(
            "INSERT INTO nodes(node_id, project_id, type, title, content, state) VALUES ('n3','p1','Entity','Indexer','indexes symbols','active')"
        )
        self.conn.execute(
            "INSERT INTO edges(edge_id, project_id, type, from_node_id, to_node_id, state) VALUES ('e1','p1','DEPENDS_ON','n3','n1','active')"
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmpdir.cleanup()

    def test_hybrid_returns_provenance_and_confidence(self) -> None:
        embed = EmbeddingWorker(self.conn, EmbeddingConfig(enabled=True, provider="hash-local", dimensions=384))
        embed.rebuild_node_embeddings("p1")
        engine = HybridRetrievalEngine(self.conn, embed, RetrievalWeights())

        out = engine.search(project_id="p1", query="parses code", limit=3)
        self.assertEqual(out["mode"], "hybrid")
        self.assertGreaterEqual(len(out["results"]), 1)
        top = out["results"][0]
        self.assertIn("provenance", top)
        self.assertIn("confidence", top)

    def test_disabled_embeddings_falls_back_with_warning(self) -> None:
        embed = EmbeddingWorker(self.conn, EmbeddingConfig(enabled=False, provider="hash-local", dimensions=384))
        engine = HybridRetrievalEngine(self.conn, embed, RetrievalWeights())
        out = engine.search(project_id="p1", query="parses", limit=3)
        self.assertEqual(out["mode"], "fts_graph")
        self.assertIn("EMBEDDING_DISABLED", out["warnings"])

    def test_deprecated_hidden_by_default(self) -> None:
        embed = EmbeddingWorker(self.conn, EmbeddingConfig(enabled=True, provider="hash-local", dimensions=384))
        embed.rebuild_node_embeddings("p1")
        engine = HybridRetrievalEngine(self.conn, embed, RetrievalWeights())

        out = engine.search(project_id="p1", query="deprecated parser", limit=3, include_deprecated=False)
        self.assertTrue(all(row["state"] == "active" for row in out["results"]))
        self.assertNotIn("n2", {row["node_id"] for row in out["results"]})

    def test_include_deprecated_true_returns_deprecated_candidates(self) -> None:
        embed = EmbeddingWorker(self.conn, EmbeddingConfig(enabled=True, provider="hash-local", dimensions=384))
        embed.rebuild_node_embeddings("p1")
        engine = HybridRetrievalEngine(self.conn, embed, RetrievalWeights())

        out = engine.search(project_id="p1", query="deprecated parser", limit=5, include_deprecated=True)
        self.assertIn("n2", {row["node_id"] for row in out["results"]})


if __name__ == "__main__":
    unittest.main()
