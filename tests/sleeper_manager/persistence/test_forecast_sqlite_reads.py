"""Verify cutoff-safe reads and storage measurements in the local forecast archive."""

import gzip
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from sleeper_manager.domain.forecast_capture import ForecastCaptureOutcome
from sleeper_manager.persistence.forecast_codec import encode_forecast_snapshot
from sleeper_manager.persistence.forecast_repository import ForecastArchiveIntegrityError
from sleeper_manager.persistence.forecast_sqlite import SQLiteForecastArchiveRepository
from tests.sleeper_manager.persistence.forecast_sqlite_support import (
    BASE,
    SOURCE,
    failed_capture,
    successful_capture,
)


def repository(tmp_path) -> SQLiteForecastArchiveRepository:  # type: ignore[no-untyped-def]
    """Create one initialized file-backed forecast archive."""

    archive = SQLiteForecastArchiveRepository(tmp_path / "forecasts.db")
    archive.initialize()
    return archive


def test_repository_round_trips_artifact_revision_and_receipt(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Recover every stored domain object through its strict decoder."""

    archive = repository(tmp_path)
    capture = successful_capture()
    archive.save_capture(capture)
    assert capture.artifact is not None
    assert capture.revision is not None

    assert archive.load_artifact(capture.artifact.payload_hash) == capture.artifact
    assert archive.load_revision(capture.revision.revision_id) == capture.revision
    assert archive.load_receipt(capture.receipt.receipt_id) == capture.receipt
    assert archive.load_artifact("f" * 64) is None
    assert archive.load_revision("missing") is None
    assert archive.load_receipt("missing") is None


def test_cutoff_reads_separate_latest_gap_from_latest_usable_revision(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Keep failed capture evidence visible without discarding the prior snapshot."""

    archive = repository(tmp_path)
    first = successful_capture(receipt_id="changed-1")
    gap = failed_capture(receipt_id="failed-2", persisted_at=BASE + timedelta(hours=1))
    second = successful_capture(
        receipt_id="changed-3",
        persisted_at=BASE + timedelta(hours=2),
        payload=b"source-payload-3",
        points=28.0,
    )
    for capture in (first, gap, second):
        archive.save_capture(capture)

    assert (
        archive.load_latest_receipt(
            SOURCE,
            cutoff=BASE - timedelta(seconds=1),
        )
        is None
    )
    assert (
        archive.load_revision_at_cutoff(
            SOURCE,
            cutoff=BASE - timedelta(seconds=1),
        )
        is None
    )

    gap_cutoff = BASE + timedelta(hours=1)
    assert archive.load_latest_receipt(SOURCE, cutoff=gap_cutoff) == gap.receipt
    earlier = archive.load_revision_at_cutoff(SOURCE, cutoff=gap_cutoff)
    assert earlier is not None
    assert earlier.receipt == first.receipt
    assert earlier.revision == first.revision

    latest = archive.load_revision_at_cutoff(
        SOURCE,
        cutoff=BASE + timedelta(hours=2),
    )
    assert latest is not None
    assert latest.receipt == second.receipt
    assert latest.revision == second.revision


def test_cutoff_reads_are_source_scoped_and_require_aware_time(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Prevent cross-source evidence and timestamps without chronological meaning."""

    archive = repository(tmp_path)
    archive.save_capture(successful_capture())
    other_source = replace(SOURCE, season="2027")

    assert archive.load_latest_receipt(other_source, cutoff=BASE) is None
    assert archive.load_revision_at_cutoff(other_source, cutoff=BASE) is None
    with pytest.raises(ValueError, match="timezone-aware"):
        archive.load_latest_receipt(SOURCE, cutoff=datetime(2026, 9, 21, 12))
    with pytest.raises(ValueError, match="timezone-aware"):
        archive.load_revision_at_cutoff(SOURCE, cutoff=datetime(2026, 9, 21, 12))


def test_cutoff_ties_use_receipt_id_for_deterministic_selection(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Resolve equal persistence timestamps identically across backends."""

    archive = repository(tmp_path)
    first = successful_capture(receipt_id="receipt-a")
    second = successful_capture(
        receipt_id="receipt-z",
        outcome=ForecastCaptureOutcome.UNCHANGED,
    )
    archive.save_capture(first)
    archive.save_capture(second)

    assert archive.load_latest_receipt(SOURCE, cutoff=BASE) == second.receipt
    selected = archive.load_revision_at_cutoff(SOURCE, cutoff=BASE)
    assert selected is not None
    assert selected.receipt == second.receipt


def test_storage_measurement_counts_deduplicated_payload_volume(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Measure retained bytes rather than multiplying unchanged capture candidates."""

    archive = repository(tmp_path)
    assert archive.measure_storage().artifact_count == 0
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
        archive.save_capture(capture)
    assert first.artifact is not None and second.artifact is not None
    assert first.revision is not None
    encoded_revision = encode_forecast_snapshot(first.revision.records)

    storage = archive.measure_storage()

    assert (storage.artifact_count, storage.revision_count, storage.receipt_count) == (2, 1, 3)
    assert storage.raw_encoded_bytes == sum(
        len(capture.artifact.encoded_payload) for capture in (first, second) if capture.artifact
    )
    assert storage.raw_uncompressed_bytes == len(b"source-payload-1") + len(
        b"metadata-only-source-change"
    )
    assert storage.revision_encoded_bytes == len(encoded_revision.encoded_records)
    assert storage.revision_uncompressed_bytes == encoded_revision.uncompressed_size


def test_list_receipts_returns_one_source_window_in_chronological_order(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Include the window start, exclude its end, and ignore other sources."""

    archive = repository(tmp_path)
    opening = successful_capture(receipt_id="receipt-z")
    middle = failed_capture(receipt_id="receipt-a", persisted_at=BASE + timedelta(hours=1))
    excluded = failed_capture(receipt_id="receipt-end", persisted_at=BASE + timedelta(hours=2))
    for capture in (middle, opening, excluded):
        archive.save_capture(capture)

    listed = archive.list_receipts(
        SOURCE,
        start=BASE,
        end=BASE + timedelta(hours=2),
    )
    other = archive.list_receipts(
        replace(SOURCE, season="2027"),
        start=BASE,
        end=BASE + timedelta(hours=2),
    )

    assert listed == (opening.receipt, middle.receipt)
    assert other == ()
    with pytest.raises(ValueError, match="timezone-aware"):
        archive.list_receipts(SOURCE, start=datetime(2026, 9, 21, 12), end=BASE)
    with pytest.raises(ValueError, match="window end"):
        archive.list_receipts(SOURCE, start=BASE, end=BASE)


def test_revision_read_rejects_corrupt_snapshot_bytes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Fail closed when normalized records no longer match stored revision evidence."""

    archive = repository(tmp_path)
    capture = successful_capture()
    archive.save_capture(capture)
    assert capture.revision is not None
    with archive._connect() as connection:
        connection.execute(
            "UPDATE forecast_revisions SET encoded_records = ?",
            (gzip.compress(b"{}", mtime=0),),
        )

    with pytest.raises(ForecastArchiveIntegrityError, match="revision row"):
        archive.load_revision(capture.revision.revision_id)
