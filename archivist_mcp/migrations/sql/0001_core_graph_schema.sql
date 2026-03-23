CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    client_type TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ended_at TEXT,
    FOREIGN KEY(project_id) REFERENCES projects(project_id),
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('Decision','Entity','Incident','Rule','Observation','Session','Artifact')),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
    content TEXT NOT NULL CHECK(length(content) BETWEEN 1 AND 20000),
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','deprecated','superseded','invalidated','archived')),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    created_by TEXT,
    supersedes_node_id TEXT,
    FOREIGN KEY(project_id) REFERENCES projects(project_id),
    FOREIGN KEY(created_by) REFERENCES users(user_id),
    FOREIGN KEY(supersedes_node_id) REFERENCES nodes(node_id)
);

CREATE TABLE IF NOT EXISTS node_properties (
    node_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(node_id, key),
    FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS edges (
    edge_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('DEPENDS_ON','BLOCKS','RESOLVED_BY','DEPRECATES','EVIDENCED_BY','DERIVED_FROM','SCOPED_TO','SIMILAR_TO')),
    from_node_id TEXT NOT NULL,
    to_node_id TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0 CHECK(weight >= 0.0 AND weight <= 1.0),
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','deprecated','superseded','invalidated','archived')),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY(project_id) REFERENCES projects(project_id),
    FOREIGN KEY(from_node_id) REFERENCES nodes(node_id),
    FOREIGN KEY(to_node_id) REFERENCES nodes(node_id),
    CHECK(from_node_id != to_node_id OR type = 'SIMILAR_TO')
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    uri TEXT NOT NULL,
    hash TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY(project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS node_artifacts (
    node_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    relationship TEXT NOT NULL,
    PRIMARY KEY(node_id, artifact_id, relationship),
    FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE,
    FOREIGN KEY(artifact_id) REFERENCES artifacts(artifact_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS observations (
    observation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    session_id TEXT,
    text TEXT NOT NULL,
    source TEXT,
    confidence REAL CHECK(confidence >= 0.0 AND confidence <= 1.0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    promoted_node_id TEXT,
    FOREIGN KEY(project_id) REFERENCES projects(project_id),
    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
    FOREIGN KEY(promoted_node_id) REFERENCES nodes(node_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    actor_id TEXT,
    action TEXT NOT NULL,
    target_id TEXT NOT NULL,
    details_json TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY(project_id) REFERENCES projects(project_id),
    FOREIGN KEY(actor_id) REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_nodes_project_type_state_updated
    ON nodes(project_id, type, state, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_edges_project_from_type
    ON edges(project_id, from_node_id, type);

CREATE INDEX IF NOT EXISTS idx_edges_project_to_type
    ON edges(project_id, to_node_id, type);

CREATE INDEX IF NOT EXISTS idx_observations_project_created
    ON observations(project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_events_project_created
    ON audit_events(project_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS ux_edges_active_tuple
    ON edges(project_id, type, from_node_id, to_node_id)
    WHERE state = 'active';
