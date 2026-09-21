"""Verify the forecast archive schema shared by local SQLite and Cloudflare D1."""

import sqlite3
from pathlib import Path

import pytest

from sleeper_manager.persistence.forecast_statements import FORECAST_ARCHIVE_SCHEMA

ROOT = Path(__file__).parents[3]
MIGRATION = ROOT / "infra" / "cloudflare" / "forecast-migrations" / "0001_forecast_archive.sql"
HASH_A = "a" * 64
HASH_B = "b" * 64
SOURCE_VALUES = (
    "sleeper",
    "/projections/nba/2026?season_type=regular",
    "2026",
    "regular",
    "season",
    "sleeper-season-forecast-v1",
)


def database() -> sqlite3.Connection:
    """Create a strict in-memory archive using the production schema."""

    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(FORECAST_ARCHIVE_SCHEMA)
    return connection


def insert_artifact(connection: sqlite3.Connection, payload_hash: str = HASH_A) -> None:
    """Insert a minimal valid immutable raw artifact."""

    connection.execute(
        """
        INSERT INTO forecast_raw_artifacts (
            payload_hash, encoding, encoded_payload, uncompressed_size, stored_at
        ) VALUES (?, 'gzip_json', ?, 12, '2026-09-20T12:00:00+00:00')
        """,
        (payload_hash, b"compressed"),
    )


def insert_revision(
    connection: sqlite3.Connection,
    *,
    revision_id: str = "revision-1",
    semantic_hash: str = HASH_B,
) -> None:
    """Insert a minimal valid normalized revision."""

    connection.execute(
        """
        INSERT INTO forecast_revisions (
            revision_id, provider, endpoint, season, season_type, horizon,
            adapter_version, semantic_hash, source_payload_hash, first_persisted_at,
            provider_updated_from, provider_updated_to, total_rows,
            numeric_forecast_rows, core_complete_rows, records_encoding,
            encoded_records, records_uncompressed_size
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 1, 1, 1,
            'gzip_json', ?, 64
        )
        """,
        (
            revision_id,
            *SOURCE_VALUES,
            semantic_hash,
            HASH_A,
            "2026-09-20T12:00:00+00:00",
            b"normalized-records",
        ),
    )


def test_schema_matches_deployable_forecast_migration() -> None:
    """Keep local repository bootstrap and the separate D1 migration identical."""

    assert MIGRATION.read_text(encoding="utf-8").strip() == FORECAST_ARCHIVE_SCHEMA.strip()


def test_schema_creates_archive_tables_and_query_indexes() -> None:
    """Create all archive structures needed for writes, cutoffs, gaps, and audits."""

    connection = database()
    objects = {
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT name, type FROM sqlite_master WHERE name LIKE 'forecast_%'"
        )
    }

    assert {
        ("forecast_raw_artifacts", "table"),
        ("forecast_revisions", "table"),
        ("forecast_fetch_receipts", "table"),
        ("forecast_raw_artifacts_stored_idx", "index"),
        ("forecast_revisions_source_time_idx", "index"),
        ("forecast_fetch_receipts_cutoff_idx", "index"),
        ("forecast_fetch_receipts_source_time_idx", "index"),
        ("forecast_fetch_receipts_gap_idx", "index"),
    }.issubset(objects)


def test_schema_stores_complete_changed_capture() -> None:
    """Accept a raw artifact, normalized revision snapshot, and usable receipt."""

    connection = database()
    insert_artifact(connection)
    insert_revision(connection)
    connection.execute(
        """
        INSERT INTO forecast_fetch_receipts (
            receipt_id, provider, endpoint, season, season_type, horizon,
            adapter_version, scheduled_for, started_at, response_received_at,
            persisted_at, outcome, timing, http_status, payload_hash,
            semantic_hash, revision_id, error_code
        ) VALUES (
            'receipt-1', ?, ?, ?, ?, ?, ?,
            '2026-09-20T11:55:00+00:00', '2026-09-20T12:00:00+00:00',
            '2026-09-20T12:00:01+00:00', '2026-09-20T12:00:02+00:00',
            'changed', 'on_time', 200, ?, ?, 'revision-1', NULL
        )
        """,
        (*SOURCE_VALUES, HASH_A, HASH_B),
    )

    assert connection.execute("SELECT COUNT(*) FROM forecast_fetch_receipts").fetchone() == (1,)
    assert connection.execute(
        "SELECT encoded_records FROM forecast_revisions WHERE revision_id = 'revision-1'"
    ).fetchone() == (b"normalized-records",)


def test_schema_deduplicates_raw_and_semantic_content() -> None:
    """Reject duplicate hashes even when callers propose different identities."""

    connection = database()
    insert_artifact(connection)
    with pytest.raises(sqlite3.IntegrityError):
        insert_artifact(connection)

    insert_revision(connection)
    with pytest.raises(sqlite3.IntegrityError):
        insert_revision(connection, revision_id="revision-2")


def test_schema_stores_failed_and_suppressed_gap_receipts() -> None:
    """Retain queryable evidence when a capture has no usable response."""

    connection = database()
    connection.execute(
        """
        INSERT INTO forecast_fetch_receipts (
            receipt_id, provider, endpoint, season, season_type, horizon,
            adapter_version, scheduled_for, started_at, persisted_at,
            outcome, timing, error_code
        ) VALUES (
            'failed-1', ?, ?, ?, ?, ?, ?, '2026-09-20T12:00:00+00:00',
            '2026-09-20T12:00:01+00:00', '2026-09-20T12:00:02+00:00',
            'failed', 'on_time', 'transport_error'
        )
        """,
        SOURCE_VALUES,
    )
    connection.execute(
        """
        INSERT INTO forecast_fetch_receipts (
            receipt_id, provider, endpoint, season, season_type, horizon,
            adapter_version, scheduled_for, persisted_at,
            outcome, timing, error_code
        ) VALUES (
            'suppressed-1', ?, ?, ?, ?, ?, ?, '2026-09-20T13:00:00+00:00',
            '2026-09-20T13:00:01+00:00', 'suppressed', 'on_time', 'daily_cap'
        )
        """,
        SOURCE_VALUES,
    )

    assert connection.execute(
        "SELECT receipt_id, outcome FROM forecast_fetch_receipts ORDER BY scheduled_for"
    ).fetchall() == [("failed-1", "failed"), ("suppressed-1", "suppressed")]


@pytest.mark.parametrize(
    "statement",
    (
        """
        INSERT INTO forecast_revisions (
            revision_id, provider, endpoint, season, season_type, horizon,
            adapter_version, semantic_hash, source_payload_hash, first_persisted_at,
            total_rows, numeric_forecast_rows, core_complete_rows,
            records_encoding, encoded_records, records_uncompressed_size
        ) VALUES (
            'bad-coverage', 'sleeper', '/forecasts', '2026', 'regular', 'season',
            'v1', 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            '2026-09-20T12:00:00+00:00', 1, 2, 0, 'gzip_json', X'01', 1
        )
        """,
        """
        INSERT INTO forecast_revisions (
            revision_id, provider, endpoint, season, season_type, horizon,
            adapter_version, semantic_hash, source_payload_hash, first_persisted_at,
            total_rows, numeric_forecast_rows, core_complete_rows,
            records_encoding, encoded_records, records_uncompressed_size
        ) VALUES (
            'empty-snapshot', 'sleeper', '/forecasts', '2026', 'regular', 'season',
            'v1', 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            '2026-09-20T12:00:00+00:00', 0, 0, 0, 'gzip_json', X'', 1
        )
        """,
        """
        INSERT INTO forecast_fetch_receipts (
            receipt_id, provider, endpoint, season, season_type, horizon,
            adapter_version, scheduled_for, persisted_at, outcome, timing, error_code
        ) VALUES (
            'bad-receipt', 'sleeper', '/forecasts', '2026', 'regular', 'season',
            'v1', '2026-09-20T12:00:00+00:00', '2026-09-20T12:00:01+00:00',
            'changed', 'on_time', NULL
        )
        """,
    ),
)
def test_schema_rejects_invalid_archive_rows(statement: str) -> None:
    """Enforce snapshot shape, coverage ordering, and receipt outcome evidence."""

    connection = database()
    insert_artifact(connection)
    insert_revision(connection)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(statement)


def test_schema_rejects_dangling_artifact_and_revision_references() -> None:
    """Prevent revisions and receipts from referencing absent evidence."""

    connection = database()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO forecast_revisions (
                revision_id, provider, endpoint, season, season_type, horizon,
                adapter_version, semantic_hash, source_payload_hash, first_persisted_at,
                total_rows, numeric_forecast_rows, core_complete_rows,
                records_encoding, encoded_records, records_uncompressed_size
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 'gzip_json', X'01', 1)
            """,
            ("revision-1", *SOURCE_VALUES, HASH_B, HASH_A, "2026-09-20T12:00:00+00:00"),
        )

    insert_artifact(connection)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO forecast_fetch_receipts (
                receipt_id, provider, endpoint, season, season_type, horizon,
                adapter_version, scheduled_for, started_at, response_received_at,
                persisted_at, outcome, timing, http_status, payload_hash,
                semantic_hash, revision_id
            ) VALUES (
                'receipt-1', ?, ?, ?, ?, ?, ?,
                '2026-09-20T11:55:00+00:00', '2026-09-20T12:00:00+00:00',
                '2026-09-20T12:00:01+00:00', '2026-09-20T12:00:02+00:00',
                'changed', 'on_time', 200, ?, ?, 'missing-revision'
            )
            """,
            (*SOURCE_VALUES, HASH_A, HASH_B),
        )
