CREATE TABLE IF NOT EXISTS hybrid_artifacts (
    artifact_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    encoded_payload TEXT NOT NULL,
    uncompressed_size INTEGER NOT NULL CHECK(uncompressed_size >= 0)
);
CREATE TABLE IF NOT EXISTS hybrid_history_versions (
    version TEXT PRIMARY KEY,
    manifest_artifact_id TEXT NOT NULL REFERENCES hybrid_artifacts(artifact_id),
    reference_time TEXT NOT NULL,
    published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hybrid_shadow_runs (
    run_id TEXT PRIMARY KEY,
    league_id TEXT NOT NULL,
    cutoff TEXT NOT NULL,
    persisted_at TEXT NOT NULL,
    manifest_artifact_id TEXT NOT NULL REFERENCES hybrid_artifacts(artifact_id)
);
CREATE INDEX IF NOT EXISTS hybrid_shadow_cutoffs ON hybrid_shadow_runs(league_id, cutoff);
