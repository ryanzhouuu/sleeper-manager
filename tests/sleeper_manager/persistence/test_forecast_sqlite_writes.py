"""Verify atomic immutable writes in the local forecast archive."""

import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.domain.forecast_capture import ForecastCaptureOutcome
from sleeper_manager.persistence.forecast_repository import (
    ForecastArchiveConflictError,
    ForecastArchiveIntegrityError,
)
from sleeper_manager.persistence.forecast_sqlite import SQLiteForecastArchiveRepository
from tests.sleeper_manager.persistence.forecast_sqlite_support import (
    BASE,
    failed_capture,
    successful_capture,
    suppressed_capture,
)


def counts(repository: SQLiteForecastArchiveRepository) -> tuple[int, int, int]:
    """Return artifact, revision, and receipt counts for transaction assertions."""

    with repository._connect() as connection:
        return tuple(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "forecast_raw_artifacts",
                "forecast_revisions",
                "forecast_fetch_receipts",
            )
        )  # type: ignore[return-value]


def repository(tmp_path) -> SQLiteForecastArchiveRepository:  # type: ignore[no-untyped-def]
    """Create one initialized file-backed forecast archive."""

    archive = SQLiteForecastArchiveRepository(tmp_path / "forecasts.db")
    archive.initialize()
    return archive


def test_changed_capture_is_inserted_and_exact_retry_is_idempotent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Write the complete evidence graph once across uncertain caller retries."""

    archive = repository(tmp_path)
    capture = successful_capture()

    created = archive.save_capture(capture)
    retried = archive.save_capture(capture)

    assert (created.artifact_created, created.revision_created, created.receipt_created) == (
        True,
        True,
        True,
    )
    assert (retried.artifact_created, retried.revision_created, retried.receipt_created) == (
        False,
        False,
        False,
    )
    assert counts(archive) == (1, 1, 1)


def test_metadata_only_capture_adds_artifact_and_receipt_but_reuses_revision(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Deduplicate semantic content while retaining a changed exact response."""

    archive = repository(tmp_path)
    first = successful_capture()
    archive.save_capture(first)
    second = successful_capture(
        receipt_id="receipt-2",
        persisted_at=BASE + timedelta(hours=1),
        payload=b"metadata-only-source-change",
        provider_updated_at=BASE + timedelta(minutes=30),
        outcome=ForecastCaptureOutcome.UNCHANGED,
    )

    result = archive.save_capture(second)

    assert (result.artifact_created, result.revision_created, result.receipt_created) == (
        True,
        False,
        True,
    )
    assert counts(archive) == (2, 1, 2)
    with archive._connect() as connection:
        stored = connection.execute(
            "SELECT source_payload_hash, first_persisted_at FROM forecast_revisions"
        ).fetchone()
    assert first.artifact is not None
    assert tuple(stored) == (first.artifact.payload_hash, BASE.isoformat())


def test_failed_and_suppressed_captures_append_receipts_only(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Persist explicit gap evidence without manufacturing source content."""

    archive = repository(tmp_path)

    failed = archive.save_capture(failed_capture())
    suppressed = archive.save_capture(suppressed_capture(persisted_at=BASE + timedelta(hours=1)))

    assert failed.receipt_created and suppressed.receipt_created
    assert not failed.artifact_created and not failed.revision_created
    assert not suppressed.artifact_created and not suppressed.revision_created
    assert counts(archive) == (0, 0, 2)


def test_changed_outcome_for_existing_revision_rolls_back(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Reject a caller that labels reused semantic content as newly changed."""

    archive = repository(tmp_path)
    archive.save_capture(successful_capture())
    mislabeled = successful_capture(
        receipt_id="receipt-2",
        persisted_at=BASE + timedelta(hours=1),
        payload=b"different-metadata",
        outcome=ForecastCaptureOutcome.CHANGED,
    )

    with pytest.raises(ForecastArchiveConflictError, match="must be 'unchanged'"):
        archive.save_capture(mislabeled)

    assert counts(archive) == (1, 1, 1)


def test_conflicting_receipt_id_rolls_back_new_content(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Leave no artifact or revision when an immutable receipt identity conflicts."""

    archive = repository(tmp_path)
    archive.save_capture(successful_capture())
    conflict = successful_capture(
        receipt_id="receipt-1",
        persisted_at=BASE + timedelta(hours=1),
        payload=b"new-source-content",
        points=29.0,
    )

    with pytest.raises(ForecastArchiveConflictError, match="receipt ID"):
        archive.save_capture(conflict)

    assert counts(archive) == (1, 1, 1)


def test_database_failure_rolls_back_artifact_and_revision(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Prove a receipt failure cannot leave earlier transaction writes visible."""

    archive = repository(tmp_path)
    with archive._connect() as connection:
        connection.executescript(
            """
            CREATE TRIGGER reject_forecast_receipt
            BEFORE INSERT ON forecast_fetch_receipts
            BEGIN
                SELECT RAISE(ABORT, 'forced receipt failure');
            END;
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced receipt failure"):
        archive.save_capture(successful_capture())

    assert counts(archive) == (0, 0, 0)


def test_deduplication_rejects_corrupt_stored_artifact(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Distinguish damaged archive bytes from a legitimate content collision."""

    archive = repository(tmp_path)
    capture = successful_capture()
    archive.save_capture(capture)
    with archive._connect() as connection:
        connection.execute(
            "UPDATE forecast_raw_artifacts SET encoded_payload = ?",
            (b"not-gzip",),
        )

    with pytest.raises(ForecastArchiveIntegrityError, match="corrupt"):
        archive.save_capture(capture)

    assert counts(archive) == (1, 1, 1)


def test_unchanged_outcome_cannot_create_first_revision(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Reject an unchanged receipt when no prior semantic revision exists."""

    archive = repository(tmp_path)
    changed = successful_capture()
    unchanged = replace(
        changed,
        receipt=replace(changed.receipt, outcome=ForecastCaptureOutcome.UNCHANGED),
    )

    with pytest.raises(ForecastArchiveConflictError, match="must be 'changed'"):
        archive.save_capture(unchanged)

    assert counts(archive) == (0, 0, 0)
