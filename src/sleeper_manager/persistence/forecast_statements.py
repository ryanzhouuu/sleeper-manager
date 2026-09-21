"""Shared SQLite and D1 schema for the isolated forecast evidence archive."""

FORECAST_ARCHIVE_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS forecast_raw_artifacts (
    payload_hash TEXT PRIMARY KEY CHECK (length(payload_hash) = 64),
    encoding TEXT NOT NULL CHECK (encoding IN ('gzip_json')),
    encoded_payload BLOB NOT NULL CHECK (length(encoded_payload) > 0),
    uncompressed_size INTEGER NOT NULL CHECK (uncompressed_size > 0),
    stored_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS forecast_raw_artifacts_stored_idx
ON forecast_raw_artifacts (stored_at, payload_hash);

CREATE TABLE IF NOT EXISTS forecast_revisions (
    revision_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    season TEXT NOT NULL,
    season_type TEXT NOT NULL,
    horizon TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    semantic_hash TEXT NOT NULL CHECK (length(semantic_hash) = 64),
    source_payload_hash TEXT NOT NULL CHECK (length(source_payload_hash) = 64),
    first_persisted_at TEXT NOT NULL,
    provider_updated_from TEXT,
    provider_updated_to TEXT,
    total_rows INTEGER NOT NULL CHECK (total_rows >= 0),
    numeric_forecast_rows INTEGER NOT NULL CHECK (
        numeric_forecast_rows >= 0 AND numeric_forecast_rows <= total_rows
    ),
    core_complete_rows INTEGER NOT NULL CHECK (
        core_complete_rows >= 0 AND core_complete_rows <= numeric_forecast_rows
    ),
    records_encoding TEXT NOT NULL CHECK (records_encoding IN ('gzip_json')),
    encoded_records BLOB NOT NULL CHECK (length(encoded_records) > 0),
    records_uncompressed_size INTEGER NOT NULL CHECK (records_uncompressed_size > 0),
    UNIQUE (
        provider,
        endpoint,
        season,
        season_type,
        horizon,
        adapter_version,
        semantic_hash
    ),
    FOREIGN KEY (source_payload_hash)
        REFERENCES forecast_raw_artifacts(payload_hash)
);

CREATE INDEX IF NOT EXISTS forecast_revisions_source_time_idx
ON forecast_revisions (
    provider,
    endpoint,
    season,
    season_type,
    horizon,
    adapter_version,
    first_persisted_at DESC
);

CREATE TABLE IF NOT EXISTS forecast_fetch_receipts (
    receipt_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    season TEXT NOT NULL,
    season_type TEXT NOT NULL,
    horizon TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    started_at TEXT,
    response_received_at TEXT,
    persisted_at TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (
        outcome IN ('changed', 'unchanged', 'failed', 'invalid', 'suppressed')
    ),
    timing TEXT NOT NULL CHECK (timing IN ('on_time', 'late')),
    http_status INTEGER CHECK (http_status BETWEEN 100 AND 599),
    payload_hash TEXT CHECK (payload_hash IS NULL OR length(payload_hash) = 64),
    semantic_hash TEXT CHECK (semantic_hash IS NULL OR length(semantic_hash) = 64),
    revision_id TEXT,
    error_code TEXT,
    CHECK (http_status IS NULL OR response_received_at IS NOT NULL),
    CHECK (payload_hash IS NULL OR response_received_at IS NOT NULL),
    CHECK (
        (
            outcome IN ('changed', 'unchanged')
            AND started_at IS NOT NULL
            AND response_received_at IS NOT NULL
            AND http_status BETWEEN 200 AND 299
            AND payload_hash IS NOT NULL
            AND semantic_hash IS NOT NULL
            AND revision_id IS NOT NULL
            AND error_code IS NULL
        ) OR (
            outcome = 'invalid'
            AND started_at IS NOT NULL
            AND response_received_at IS NOT NULL
            AND http_status BETWEEN 200 AND 299
            AND payload_hash IS NOT NULL
            AND semantic_hash IS NULL
            AND revision_id IS NULL
            AND error_code IS NOT NULL
        ) OR (
            outcome = 'failed'
            AND started_at IS NOT NULL
            AND semantic_hash IS NULL
            AND revision_id IS NULL
            AND error_code IS NOT NULL
        ) OR (
            outcome = 'suppressed'
            AND started_at IS NULL
            AND response_received_at IS NULL
            AND http_status IS NULL
            AND payload_hash IS NULL
            AND semantic_hash IS NULL
            AND revision_id IS NULL
            AND error_code IS NOT NULL
        )
    ),
    FOREIGN KEY (payload_hash) REFERENCES forecast_raw_artifacts(payload_hash),
    FOREIGN KEY (revision_id) REFERENCES forecast_revisions(revision_id)
);

CREATE INDEX IF NOT EXISTS forecast_fetch_receipts_cutoff_idx
ON forecast_fetch_receipts (
    provider,
    endpoint,
    season,
    season_type,
    horizon,
    adapter_version,
    persisted_at DESC,
    receipt_id DESC
)
WHERE outcome IN ('changed', 'unchanged');

CREATE INDEX IF NOT EXISTS forecast_fetch_receipts_source_time_idx
ON forecast_fetch_receipts (
    provider,
    endpoint,
    season,
    season_type,
    horizon,
    adapter_version,
    persisted_at DESC,
    receipt_id DESC
);

CREATE INDEX IF NOT EXISTS forecast_fetch_receipts_gap_idx
ON forecast_fetch_receipts (provider, season, scheduled_for DESC, outcome);
"""

INSERT_FORECAST_ARTIFACT_SQL = """
INSERT OR IGNORE INTO forecast_raw_artifacts (
    payload_hash, encoding, encoded_payload, uncompressed_size, stored_at
) VALUES (?, ?, ?, ?, ?)
"""

LOAD_FORECAST_ARTIFACT_SQL = """
SELECT payload_hash, encoding, encoded_payload, uncompressed_size, stored_at
FROM forecast_raw_artifacts
WHERE payload_hash = ?
"""

INSERT_FORECAST_REVISION_SQL = """
INSERT OR IGNORE INTO forecast_revisions (
    revision_id, provider, endpoint, season, season_type, horizon,
    adapter_version, semantic_hash, source_payload_hash, first_persisted_at,
    provider_updated_from, provider_updated_to, total_rows,
    numeric_forecast_rows, core_complete_rows, records_encoding,
    encoded_records, records_uncompressed_size
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

LOAD_FORECAST_REVISION_SQL = """
SELECT
    revision_id, provider, endpoint, season, season_type, horizon,
    adapter_version, semantic_hash, source_payload_hash, first_persisted_at,
    provider_updated_from, provider_updated_to, total_rows,
    numeric_forecast_rows, core_complete_rows, records_encoding,
    encoded_records, records_uncompressed_size
FROM forecast_revisions
WHERE revision_id = ?
"""

INSERT_FORECAST_RECEIPT_SQL = """
INSERT OR IGNORE INTO forecast_fetch_receipts (
    receipt_id, provider, endpoint, season, season_type, horizon,
    adapter_version, scheduled_for, started_at, response_received_at,
    persisted_at, outcome, timing, http_status, payload_hash,
    semantic_hash, revision_id, error_code
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

LOAD_FORECAST_RECEIPT_SQL = """
SELECT
    receipt_id, provider, endpoint, season, season_type, horizon,
    adapter_version, scheduled_for, started_at, response_received_at,
    persisted_at, outcome, timing, http_status, payload_hash,
    semantic_hash, revision_id, error_code
FROM forecast_fetch_receipts
WHERE receipt_id = ?
"""

LOAD_LATEST_FORECAST_RECEIPT_SQL = """
SELECT
    receipt_id, provider, endpoint, season, season_type, horizon,
    adapter_version, scheduled_for, started_at, response_received_at,
    persisted_at, outcome, timing, http_status, payload_hash,
    semantic_hash, revision_id, error_code
FROM forecast_fetch_receipts
WHERE provider = ? AND endpoint = ? AND season = ? AND season_type = ?
  AND horizon = ? AND adapter_version = ? AND persisted_at <= ?
ORDER BY persisted_at DESC, receipt_id DESC
LIMIT 1
"""

LOAD_LATEST_USABLE_FORECAST_RECEIPT_SQL = """
SELECT
    receipt_id, provider, endpoint, season, season_type, horizon,
    adapter_version, scheduled_for, started_at, response_received_at,
    persisted_at, outcome, timing, http_status, payload_hash,
    semantic_hash, revision_id, error_code
FROM forecast_fetch_receipts
WHERE provider = ? AND endpoint = ? AND season = ? AND season_type = ?
  AND horizon = ? AND adapter_version = ? AND persisted_at <= ?
  AND outcome IN ('changed', 'unchanged')
ORDER BY persisted_at DESC, receipt_id DESC
LIMIT 1
"""

MEASURE_FORECAST_STORAGE_SQL = """
SELECT
    (SELECT COUNT(*) FROM forecast_raw_artifacts) AS artifact_count,
    (SELECT COUNT(*) FROM forecast_revisions) AS revision_count,
    (SELECT COUNT(*) FROM forecast_fetch_receipts) AS receipt_count,
    COALESCE((SELECT SUM(length(encoded_payload)) FROM forecast_raw_artifacts), 0)
        AS raw_encoded_bytes,
    COALESCE((SELECT SUM(uncompressed_size) FROM forecast_raw_artifacts), 0)
        AS raw_uncompressed_bytes,
    COALESCE((SELECT SUM(length(encoded_records)) FROM forecast_revisions), 0)
        AS revision_encoded_bytes,
    COALESCE((SELECT SUM(records_uncompressed_size) FROM forecast_revisions), 0)
        AS revision_uncompressed_bytes
"""
