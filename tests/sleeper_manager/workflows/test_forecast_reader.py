"""Verify cutoff selection of one player's forecast and a newer failed attempt."""

import gzip
from datetime import timedelta
from hashlib import sha256

import pytest

from sleeper_manager.domain.forecast_capture import (
    ForecastArtifactEncoding,
    ForecastCaptureError,
    ForecastCaptureOutcome,
    ForecastCaptureTiming,
    ForecastFetchReceipt,
    ForecastRetrievalStatus,
    RawForecastArtifact,
)
from sleeper_manager.persistence.forecast_repository import ForecastCaptureWrite
from sleeper_manager.persistence.forecast_sqlite import SQLiteForecastArchiveRepository
from sleeper_manager.workflows.forecast_reader import read_player_forecasts
from tests.sleeper_manager.persistence.forecast_sqlite_support import (
    BASE,
    SOURCE,
    failed_capture,
    successful_capture,
    suppressed_capture,
)


def test_available_forecast_keeps_a_newer_failure_without_falling_back(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Use the newest revision that still has the player and attach a later miss."""

    archive = _archive(tmp_path)
    first = successful_capture(receipt_id="monday", persisted_at=BASE, points=10)
    dropped = successful_capture(
        receipt_id="tuesday",
        persisted_at=BASE + timedelta(hours=1),
        payload=b"source-payload-2",
        player_id="player-2",
        points=4,
    )
    failed = failed_capture(receipt_id="wednesday", persisted_at=BASE + timedelta(hours=2))
    for capture in (first, dropped, failed):
        archive.save_capture(capture)

    early = read_player_forecasts(archive, SOURCE, ("player-1",), BASE)
    latest = read_player_forecasts(
        archive,
        SOURCE,
        ("player-1", "player-2"),
        BASE + timedelta(hours=2),
    )

    assert early[0].status is ForecastRetrievalStatus.AVAILABLE
    assert early[0].forecast is not None and early[0].forecast.stat("pts") == 10
    assert early[0].newer_attempt_receipt_id is None
    assert latest[0].status is ForecastRetrievalStatus.MISSING
    assert latest[0].forecast is None
    assert latest[0].provenance is not None
    assert latest[0].provenance.receipt_id == "tuesday"
    assert latest[0].coverage == dropped.revision.coverage
    assert latest[0].newer_attempt_receipt_id == "wednesday"
    assert latest[1].status is ForecastRetrievalStatus.AVAILABLE
    assert latest[1].forecast is not None and latest[1].forecast.player_id == "player-2"
    assert latest[1].newer_attempt_receipt_id == "wednesday"
    assert latest[1].age == timedelta(hours=1)


def test_gap_and_invalid_receipts_supply_no_forecast_fields(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A capture miss stays a gap or invalid result instead of an older row."""

    archive = _archive(tmp_path)
    archive.save_capture(suppressed_capture(persisted_at=BASE))
    suppressed = read_player_forecasts(archive, SOURCE, ("player-1",), BASE)
    archive.save_capture(_invalid(BASE + timedelta(hours=1)))
    invalid = read_player_forecasts(archive, SOURCE, ("player-1",), BASE + timedelta(hours=1))

    assert suppressed[0].status is ForecastRetrievalStatus.GAP
    assert suppressed[0].forecast is None
    assert suppressed[0].evidence_receipt_id == "suppressed-1"
    assert invalid[0].status is ForecastRetrievalStatus.INVALID
    assert invalid[0].evidence_receipt_id == "invalid-1"
    assert invalid[0].newer_attempt_receipt_id is None


def test_reader_ignores_attempts_persisted_after_the_cutoff(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A later failure cannot change the forecast selected for an earlier decision."""

    archive = _archive(tmp_path)
    archive.save_capture(successful_capture(persisted_at=BASE))
    archive.save_capture(failed_capture(persisted_at=BASE + timedelta(hours=3)))
    selected = read_player_forecasts(archive, SOURCE, ("player-1",), BASE + timedelta(hours=1))

    assert selected[0].status is ForecastRetrievalStatus.AVAILABLE
    assert selected[0].newer_attempt_receipt_id is None


def test_reader_requires_a_visible_receipt(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Do not invent gap evidence when the archive has no attempt by the cutoff."""

    archive = _archive(tmp_path)
    with pytest.raises(ForecastCaptureError, match="visible"):
        read_player_forecasts(archive, SOURCE, ("player-1",), BASE)


def _archive(tmp_path) -> SQLiteForecastArchiveRepository:  # type: ignore[no-untyped-def]
    """Create an initialized local archive."""

    archive = SQLiteForecastArchiveRepository(tmp_path / "forecasts.db")
    archive.initialize()
    return archive


def _invalid(persisted_at) -> ForecastCaptureWrite:  # type: ignore[no-untyped-def]
    """Build an invalid capture that retains raw bytes and no revision."""

    payload = b"not-json"
    payload_hash = sha256(payload).hexdigest()
    encoded = gzip.compress(payload, compresslevel=6, mtime=0)
    artifact = RawForecastArtifact(
        payload_hash=payload_hash,
        encoding=ForecastArtifactEncoding.GZIP_JSON,
        encoded_payload=encoded,
        uncompressed_size=len(payload),
        stored_at=persisted_at,
    )
    return ForecastCaptureWrite(
        ForecastFetchReceipt(
            receipt_id="invalid-1",
            source=SOURCE,
            scheduled_for=persisted_at - timedelta(minutes=5),
            started_at=persisted_at - timedelta(minutes=2),
            response_received_at=persisted_at - timedelta(minutes=1),
            persisted_at=persisted_at,
            outcome=ForecastCaptureOutcome.INVALID,
            timing=ForecastCaptureTiming.ON_TIME,
            http_status=200,
            payload_hash=payload_hash,
            error_code="invalid_payload",
        ),
        artifact,
    )
