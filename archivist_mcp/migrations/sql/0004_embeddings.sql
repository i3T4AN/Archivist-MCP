CREATE TABLE IF NOT EXISTS node_embeddings (
    node_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS observation_embeddings (
    observation_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY(observation_id) REFERENCES observations(observation_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_node_embeddings_updated ON node_embeddings(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_observation_embeddings_updated ON observation_embeddings(updated_at DESC);
