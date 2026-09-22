"""D1 transaction guards for immutable forecast capture batches.

Each guard inserts no row when the stored identity matches. On disagreement it
attempts a duplicate immutable insert, making D1 roll back the entire batch.
"""

ASSERT_FORECAST_ARTIFACT_SQL = """
INSERT INTO forecast_raw_artifacts (
    payload_hash, encoding, encoded_payload, uncompressed_size, stored_at
)
SELECT ?1, ?2, ?3, ?4, ?5
WHERE EXISTS (
    SELECT 1
    FROM forecast_raw_artifacts
    WHERE payload_hash = ?1
      AND NOT (
          encoding IS ?2
          AND uncompressed_size IS ?4
      )
)
"""

ASSERT_FORECAST_OUTCOME_SQL = """
INSERT INTO forecast_raw_artifacts (
    payload_hash, encoding, encoded_payload, uncompressed_size, stored_at
)
SELECT ?1, ?2, ?3, ?4, ?5
WHERE NOT EXISTS (
    SELECT 1 FROM forecast_fetch_receipts WHERE receipt_id = ?6
)
AND (
    (?7 = 'changed' AND EXISTS (
        SELECT 1 FROM forecast_revisions WHERE revision_id = ?8
    ))
    OR
    (?7 = 'unchanged' AND NOT EXISTS (
        SELECT 1 FROM forecast_revisions WHERE revision_id = ?8
    ))
)
"""

ASSERT_FORECAST_REVISION_SQL = """
INSERT INTO forecast_revisions (
    revision_id, provider, endpoint, season, season_type, horizon,
    adapter_version, semantic_hash, source_payload_hash, first_persisted_at,
    provider_updated_from, provider_updated_to, total_rows,
    numeric_forecast_rows, core_complete_rows, records_encoding,
    encoded_records, records_uncompressed_size
)
SELECT
    ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10,
    ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18
WHERE NOT EXISTS (
    SELECT 1
    FROM forecast_revisions
    WHERE revision_id = ?1
      AND provider IS ?2
      AND endpoint IS ?3
      AND season IS ?4
      AND season_type IS ?5
      AND horizon IS ?6
      AND adapter_version IS ?7
      AND semantic_hash IS ?8
)
"""

ASSERT_FORECAST_RECEIPT_SQL = """
INSERT INTO forecast_fetch_receipts (
    receipt_id, provider, endpoint, season, season_type, horizon,
    adapter_version, scheduled_for, started_at, response_received_at,
    persisted_at, outcome, timing, http_status, payload_hash,
    semantic_hash, revision_id, error_code
)
SELECT
    ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10,
    ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18
WHERE NOT EXISTS (
    SELECT 1
    FROM forecast_fetch_receipts
    WHERE receipt_id IS ?1
      AND provider IS ?2
      AND endpoint IS ?3
      AND season IS ?4
      AND season_type IS ?5
      AND horizon IS ?6
      AND adapter_version IS ?7
      AND scheduled_for IS ?8
      AND started_at IS ?9
      AND response_received_at IS ?10
      AND persisted_at IS ?11
      AND outcome IS ?12
      AND timing IS ?13
      AND http_status IS ?14
      AND payload_hash IS ?15
      AND semantic_hash IS ?16
      AND revision_id IS ?17
      AND error_code IS ?18
)
"""

LOAD_FORECAST_REVISION_BY_SEMANTIC_SQL = """
SELECT
    revision_id, provider, endpoint, season, season_type, horizon,
    adapter_version, semantic_hash, source_payload_hash, first_persisted_at,
    provider_updated_from, provider_updated_to, total_rows,
    numeric_forecast_rows, core_complete_rows, records_encoding,
    encoded_records, records_uncompressed_size
FROM forecast_revisions
WHERE provider = ? AND endpoint = ? AND season = ? AND season_type = ?
  AND horizon = ? AND adapter_version = ? AND semantic_hash = ?
"""

__all__ = (
    "ASSERT_FORECAST_ARTIFACT_SQL",
    "ASSERT_FORECAST_OUTCOME_SQL",
    "ASSERT_FORECAST_RECEIPT_SQL",
    "ASSERT_FORECAST_REVISION_SQL",
    "LOAD_FORECAST_REVISION_BY_SEMANTIC_SQL",
)
