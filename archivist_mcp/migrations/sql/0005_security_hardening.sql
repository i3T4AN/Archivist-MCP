ALTER TABLE observations ADD COLUMN needs_triage INTEGER NOT NULL DEFAULT 0 CHECK(needs_triage IN (0, 1));

CREATE INDEX IF NOT EXISTS idx_observations_project_triage_created
    ON observations(project_id, needs_triage, created_at DESC);
