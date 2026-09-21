"""Verify atomic immutable writes through the forecast D1 repository."""

import asyncio
import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.domain.forecast_capture import ForecastCaptureOutcome
from sleeper_manager.persistence.forecast_d1 import D1ForecastArchiveRepository
from sleeper_manager.persistence.forecast_repository import (
    ForecastArchiveConflictError,
    ForecastArchiveIntegrityError,
)
from sleeper_manager.persistence.forecast_statements import FORECAST_ARCHIVE_SCHEMA
from tests.sleeper_manager.persistence.forecast_sqlite_support import (
    BASE,
    failed_capture,
    successful_capture,
    suppressed_capture,
)
from tests.sleeper_manager.persistence.test_d1 import FakeD1


def repository() -> tuple[FakeD1, D1ForecastArchiveRepository]:
    """Create one initialized in-memory D1 test binding and repository."""

    database = FakeD1()
    asyncio.run(database.exec(FORECAST_ARCHIVE_SCHEMA))
    return database, D1ForecastArchiveRepository(database)


def counts(database: FakeD1) -> tuple[int, int, int]:
    """Return artifact, revision, and receipt counts for transaction assertions."""

    return tuple(
        int(database.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "forecast_raw_artifacts",
            "forecast_revisions",
            "forecast_fetch_receipts",
        )
    )  # type: ignore[return-value]


def test_changed_capture_is_inserted_and_exact_retry_is_idempotent() -> None:
    """Write the complete evidence graph once across uncertain caller retries."""

    database, archive = repository()
    capture = successful_capture()

    created = asyncio.run(archive.save_capture(capture))
    retried = asyncio.run(archive.save_capture(capture))

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
    assert counts(database) == (1, 1, 1)


def test_metadata_only_capture_adds_artifact_and_receipt_but_reuses_revision() -> None:
    """Deduplicate semantic content while retaining a changed exact response."""

    database, archive = repository()
    first = successful_capture()
    asyncio.run(archive.save_capture(first))
    second = successful_capture(
        receipt_id="receipt-2",
        persisted_at=BASE + timedelta(hours=1),
        payload=b"metadata-only-source-change",
        provider_updated_at=BASE + timedelta(minutes=30),
        outcome=ForecastCaptureOutcome.UNCHANGED,
    )

    result = asyncio.run(archive.save_capture(second))

    assert (result.artifact_created, result.revision_created, result.receipt_created) == (
        True,
        False,
        True,
    )
    assert counts(database) == (2, 1, 2)


def test_failed_and_suppressed_captures_append_receipts_only() -> None:
    """Persist explicit gap evidence without manufacturing source content."""

    database, archive = repository()

    failed = asyncio.run(archive.save_capture(failed_capture()))
    suppressed = asyncio.run(
        archive.save_capture(suppressed_capture(persisted_at=BASE + timedelta(hours=1)))
    )

    assert failed.receipt_created and suppressed.receipt_created
    assert not failed.artifact_created and not failed.revision_created
    assert not suppressed.artifact_created and not suppressed.revision_created
    assert counts(database) == (0, 0, 2)


def test_changed_outcome_for_existing_revision_rolls_back() -> None:
    """Reject a caller that labels reused semantic content as newly changed."""

    database, archive = repository()
    asyncio.run(archive.save_capture(successful_capture()))
    mislabeled = successful_capture(
        receipt_id="receipt-2",
        persisted_at=BASE + timedelta(hours=1),
        payload=b"different-metadata",
        outcome=ForecastCaptureOutcome.CHANGED,
    )

    with pytest.raises(ForecastArchiveConflictError, match="must be 'unchanged'"):
        asyncio.run(archive.save_capture(mislabeled))

    assert counts(database) == (1, 1, 1)


def test_conflicting_receipt_id_rolls_back_new_content() -> None:
    """Leave no artifact or revision when an immutable receipt identity conflicts."""

    database, archive = repository()
    asyncio.run(archive.save_capture(successful_capture()))
    conflict = successful_capture(
        receipt_id="receipt-1",
        persisted_at=BASE + timedelta(hours=1),
        payload=b"new-source-content",
        points=29.0,
    )

    with pytest.raises(ForecastArchiveConflictError, match="receipt ID"):
        asyncio.run(archive.save_capture(conflict))

    assert counts(database) == (1, 1, 1)


def test_database_failure_rolls_back_artifact_and_revision() -> None:
    """Prove a receipt failure cannot leave earlier batch writes visible."""

    database, archive = repository()
    database.connection.executescript(
        """
        CREATE TRIGGER reject_forecast_receipt
        BEFORE INSERT ON forecast_fetch_receipts
        BEGIN
            SELECT RAISE(ABORT, 'forced receipt failure');
        END;
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="forced receipt failure"):
        asyncio.run(archive.save_capture(successful_capture()))

    assert counts(database) == (0, 0, 0)


def test_deduplication_rejects_corrupt_stored_artifact() -> None:
    """Distinguish damaged archive bytes from a legitimate content collision."""

    database, archive = repository()
    capture = successful_capture()
    asyncio.run(archive.save_capture(capture))
    database.connection.execute(
        "UPDATE forecast_raw_artifacts SET encoded_payload = ?",
        (b"not-gzip",),
    )
    database.connection.commit()

    with pytest.raises(ForecastArchiveIntegrityError, match="corrupt"):
        asyncio.run(archive.save_capture(capture))

    assert counts(database) == (1, 1, 1)


def test_revision_load_rejects_corrupt_stored_snapshot() -> None:
    """Fail closed when a stored semantic snapshot no longer matches its hash."""

    database, archive = repository()
    capture = successful_capture()
    asyncio.run(archive.save_capture(capture))
    assert capture.revision is not None
    database.connection.execute(
        "UPDATE forecast_revisions SET encoded_records = ?",
        (b"not-gzip",),
    )
    database.connection.commit()

    with pytest.raises(ForecastArchiveIntegrityError, match="revision row"):
        asyncio.run(archive.load_revision(capture.revision.revision_id))


def test_unchanged_outcome_cannot_create_first_revision() -> None:
    """Reject an unchanged receipt when no prior semantic revision exists."""

    database, archive = repository()
    changed = successful_capture()
    unchanged = replace(
        changed,
        receipt=replace(changed.receipt, outcome=ForecastCaptureOutcome.UNCHANGED),
    )

    with pytest.raises(ForecastArchiveConflictError, match="must be 'changed'"):
        asyncio.run(archive.save_capture(unchanged))

    assert counts(database) == (0, 0, 0)


class _JsProxy:
    """Minimal Python Worker proxy that converts through `to_py()`."""

    def __init__(self, value: object) -> None:
        self._value = value

    def to_py(self) -> object:
        """Expose the wrapped JavaScript-shaped value to repository code."""

        return self._value


class _ProxyBatchD1(FakeD1):
    """Return the D1 batch result through a Python Worker proxy."""

    async def batch(self, statements):  # type: ignore[no-untyped-def]
        """Wrap the otherwise-real transactional fake result."""

        return _JsProxy(await super().batch(statements))


def test_d1_batch_accepts_python_worker_proxy_envelope() -> None:
    """Decode the JavaScript proxy returned by a Python Worker D1 binding."""

    database = _ProxyBatchD1()
    asyncio.run(database.exec(FORECAST_ARCHIVE_SCHEMA))
    archive = D1ForecastArchiveRepository(database)

    result = asyncio.run(archive.save_capture(successful_capture()))

    assert result.receipt_created
    assert counts(database) == (1, 1, 1)
