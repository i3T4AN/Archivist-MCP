CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    node_id UNINDEXED,
    title,
    content,
    tokenize='unicode61'
);

INSERT INTO nodes_fts(node_id, title, content)
SELECT node_id, title, content FROM nodes
WHERE node_id NOT IN (SELECT node_id FROM nodes_fts);

CREATE TRIGGER IF NOT EXISTS trg_nodes_ai AFTER INSERT ON nodes BEGIN
    INSERT INTO nodes_fts(node_id, title, content)
    VALUES (new.node_id, new.title, new.content);
END;

CREATE TRIGGER IF NOT EXISTS trg_nodes_au AFTER UPDATE OF title, content ON nodes BEGIN
    UPDATE nodes_fts
    SET title = new.title,
        content = new.content
    WHERE node_id = new.node_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_nodes_ad AFTER DELETE ON nodes BEGIN
    DELETE FROM nodes_fts WHERE node_id = old.node_id;
END;

CREATE TABLE IF NOT EXISTS idempotency_keys (
    project_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(project_id, tool_name, idempotency_key),
    FOREIGN KEY(project_id) REFERENCES projects(project_id)
);
