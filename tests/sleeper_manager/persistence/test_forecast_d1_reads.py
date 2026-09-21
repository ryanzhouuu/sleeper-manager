"""Verify cutoff-safe reads and measurements through the forecast D1 repository."""

import asyncio
import gzip
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from sleeper_manager.domain.forecast_capture import ForecastCaptureOutcome
from sleeper_manager.persistence.forecast_codec import encode_forecast_snapshot
from sleeper_manager.persistence.forecast_d1 import D1ForecastArchiveRepository
from sleeper_manager.persistence.forecast_repository import (
    AsyncForecastArchiveRepository,
    ForecastArchiveIntegrityError,
)
from sleeper_manager.persistence.forecast_statements import FORECAST_ARCHIVE_SCHEMA
from tests.sleeper_manager.persistence.forecast_sqlite_support import (
    BASE,
    SOURCE,
    failed_capture,
    successful_capture,
    suppressed_capture,
)
from tests.sleeper_manager.persistence.test_d1 import FakeD1


def repository() -> tuple[FakeD1, AsyncForecastArchiveRepository]:
    """Create one initialized in-memory D1 test binding and repository."""

    database = FakeD1()
    asyncio.run(database.exec(FORECAST_ARCHIVE_SCHEMA))
    archive: AsyncForecastArchiveRepository = D1ForecastArchiveRepository(database)
    return database, archive


def test_repository_round_trips_artifact_revision_and_receipt() -> None:
    """Recover every stored domain object through its strict decoder."""

    _, archive = repository()
    capture = successful_capture()
    asyncio.run(archive.save_capture(capture))
    assert capture.artifact is not None
    assert capture.revision is not None

    assert asyncio.run(archive.load_artifact(capture.artifact.payload_hash)) == capture.artifact
    assert asyncio.run(archive.load_revision(capture.revision.revision_id)) == capture.revision
    assert asyncio.run(archive.load_receipt(capture.receipt.receipt_id)) == capture.receipt
    assert asyncio.run(archive.load_artifact("f" * 64)) is None
    assert asyncio.run(archive.load_revision("missing")) is None
    assert asyncio.run(archive.load_receipt("missing")) is None


def test_cutoff_reads_separate_latest_gaps_from_latest_usable_revision() -> None:
    """Keep failed and suppressed attempts visible without discarding prior evidence."""

    _, archive = repository()
    first = successful_capture(receipt_id="changed-1")
    failed = failed_capture(receipt_id="failed-2", persisted_at=BASE + timedelta(hours=1))
    suppressed = suppressed_capture(
        receipt_id="suppressed-3",
        persisted_at=BASE + timedelta(hours=2),
    )
    second = successful_capture(
        receipt_id="changed-4",
        persisted_at=BASE + timedelta(hours=3),
        payload=b"source-payload-4",
        points=28.0,
    )
    for capture in (first, failed, suppressed, second):
        asyncio.run(archive.save_capture(capture))

    assert (
        asyncio.run(archive.load_latest_receipt(SOURCE, cutoff=BASE - timedelta(seconds=1))) is None
    )
    assert (
        asyncio.run(archive.load_revision_at_cutoff(SOURCE, cutoff=BASE - timedelta(seconds=1)))
        is None
    )

    failed_cutoff = BASE + timedelta(hours=1)
    assert asyncio.run(archive.load_latest_receipt(SOURCE, cutoff=failed_cutoff)) == failed.receipt
    earlier = asyncio.run(archive.load_revision_at_cutoff(SOURCE, cutoff=failed_cutoff))
    assert earlier is not None
    assert earlier.receipt == first.receipt
    assert earlier.revision == first.revision

    suppressed_cutoff = BASE + timedelta(hours=2)
    assert (
        asyncio.run(archive.load_latest_receipt(SOURCE, cutoff=suppressed_cutoff))
        == suppressed.receipt
    )
    still_earlier = asyncio.run(archive.load_revision_at_cutoff(SOURCE, cutoff=suppressed_cutoff))
    assert still_earlier is not None and still_earlier.receipt == first.receipt

    latest = asyncio.run(archive.load_revision_at_cutoff(SOURCE, cutoff=BASE + timedelta(hours=3)))
    assert latest is not None
    assert latest.receipt == second.receipt
    assert latest.revision == second.revision


def test_cutoff_reads_are_source_scoped_and_require_aware_time() -> None:
    """Prevent cross-source evidence and timestamps without chronological meaning."""

    _, archive = repository()
    asyncio.run(archive.save_capture(successful_capture()))
    other_source = replace(SOURCE, season="2027")

    assert asyncio.run(archive.load_latest_receipt(other_source, cutoff=BASE)) is None
    assert asyncio.run(archive.load_revision_at_cutoff(other_source, cutoff=BASE)) is None
    with pytest.raises(ValueError, match="timezone-aware"):
        asyncio.run(archive.load_latest_receipt(SOURCE, cutoff=datetime(2026, 9, 21, 12)))
    with pytest.raises(ValueError, match="timezone-aware"):
        asyncio.run(archive.load_revision_at_cutoff(SOURCE, cutoff=datetime(2026, 9, 21, 12)))


def test_cutoff_ties_use_receipt_id_for_deterministic_selection() -> None:
    """Resolve equal persistence timestamps identically across backends."""

    _, archive = repository()
    first = successful_capture(receipt_id="receipt-a")
    second = successful_capture(
        receipt_id="receipt-z",
        outcome=ForecastCaptureOutcome.UNCHANGED,
    )
    asyncio.run(archive.save_capture(first))
    asyncio.run(archive.save_capture(second))

    assert asyncio.run(archive.load_latest_receipt(SOURCE, cutoff=BASE)) == second.receipt
    selected = asyncio.run(archive.load_revision_at_cutoff(SOURCE, cutoff=BASE))
    assert selected is not None
    assert selected.receipt == second.receipt


def test_storage_measurement_counts_deduplicated_payload_volume() -> None:
    """Measure retained bytes rather than multiplying unchanged capture candidates."""

    _, archive = repository()
    assert asyncio.run(archive.measure_storage()).artifact_count == 0
    first = successful_capture()
    second = successful_capture(
        receipt_id="receipt-2",
        persisted_at=BASE + timedelta(hours=1),
        payload=b"metadata-only-source-change",
        provider_updated_at=BASE + timedelta(minutes=30),
        outcome=ForecastCaptureOutcome.UNCHANGED,
    )
    gap = failed_capture(receipt_id="failed-3", persisted_at=BASE + timedelta(hours=2))
    for capture in (first, second, gap):
        asyncio.run(archive.save_capture(capture))
    assert first.artifact is not None and second.artifact is not None
    assert first.revision is not None
    encoded_revision = encode_forecast_snapshot(first.revision.records)

    storage = asyncio.run(archive.measure_storage())

    assert (storage.artifact_count, storage.revision_count, storage.receipt_count) == (2, 1, 3)
    assert storage.raw_encoded_bytes == sum(
        len(capture.artifact.encoded_payload) for capture in (first, second) if capture.artifact
    )
    assert storage.raw_uncompressed_bytes == len(b"source-payload-1") + len(
        b"metadata-only-source-change"
    )
    assert storage.revision_encoded_bytes == len(encoded_revision.encoded_records)
    assert storage.revision_uncompressed_bytes == encoded_revision.uncompressed_size


def test_revision_read_rejects_corrupt_snapshot_bytes() -> None:
    """Fail closed when normalized records no longer match stored revision evidence."""

    database, archive = repository()
    capture = successful_capture()
    asyncio.run(archive.save_capture(capture))
    assert capture.revision is not None
    database.connection.execute(
        "UPDATE forecast_revisions SET encoded_records = ?",
        (gzip.compress(b"{}", mtime=0),),
    )
    database.connection.commit()

    with pytest.raises(ForecastArchiveIntegrityError, match="revision row"):
        asyncio.run(archive.load_revision(capture.revision.revision_id))


def test_cutoff_read_rejects_missing_referenced_revision() -> None:
    """Fail closed when a usable receipt outlives its referenced evidence."""

    database, archive = repository()
    capture = successful_capture()
    asyncio.run(archive.save_capture(capture))
    database.connection.execute("PRAGMA foreign_keys = OFF")
    database.connection.execute("DELETE FROM forecast_revisions")
    database.connection.commit()
    database.connection.execute("PRAGMA foreign_keys = ON")

    with pytest.raises(ForecastArchiveIntegrityError, match="revision is missing"):
        asyncio.run(archive.load_revision_at_cutoff(SOURCE, cutoff=BASE))
