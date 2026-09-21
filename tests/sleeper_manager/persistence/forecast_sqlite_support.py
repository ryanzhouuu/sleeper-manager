"""Shared fixtures for forecast archive repository tests."""

import gzip
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from sleeper_manager.domain.forecast_capture import (
    ForecastArtifactEncoding,
    ForecastCaptureOutcome,
    ForecastCaptureTiming,
    ForecastCoverage,
    ForecastFetchReceipt,
    ForecastSource,
    NormalizedForecastRevision,
    NormalizedPlayerForecast,
    RawForecastArtifact,
)
from sleeper_manager.domain.forecast_revision_identity import forecast_semantic_hash
from sleeper_manager.persistence.forecast_repository import ForecastCaptureWrite

BASE = datetime(2026, 9, 21, 12, tzinfo=UTC)
SOURCE = ForecastSource(
    provider="sleeper",
    endpoint="/projections/nba/2026?season_type=regular",
    season="2026",
    season_type="regular",
    horizon="season",
    adapter_version="sleeper-season-forecast-v1",
)


def successful_capture(
    *,
    receipt_id: str = "receipt-1",
    persisted_at: datetime = BASE,
    payload: bytes = b"source-payload-1",
    points: float = 21.5,
    provider_updated_at: datetime | None = BASE - timedelta(hours=1),
    outcome: ForecastCaptureOutcome = ForecastCaptureOutcome.CHANGED,
) -> ForecastCaptureWrite:
    """Build a complete valid capture with one normalized player."""

    payload_hash = sha256(payload).hexdigest()
    artifact = RawForecastArtifact(
        payload_hash=payload_hash,
        encoding=ForecastArtifactEncoding.GZIP_JSON,
        encoded_payload=gzip.compress(payload, compresslevel=6, mtime=0),
        uncompressed_size=len(payload),
        stored_at=persisted_at,
    )
    records = (
        NormalizedPlayerForecast(
            player_id="player-1",
            company="sleeper",
            team_id="CHI",
            stats=(("pts", points), ("ast", 5.0)),
            provider_updated_at=provider_updated_at,
        ),
    )
    semantic_hash = forecast_semantic_hash(SOURCE, records)
    revision = NormalizedForecastRevision(
        revision_id=f"{SOURCE.adapter_version}:{semantic_hash}",
        source=SOURCE,
        semantic_hash=semantic_hash,
        source_payload_hash=payload_hash,
        first_persisted_at=persisted_at,
        provider_updated_from=provider_updated_at,
        provider_updated_to=provider_updated_at,
        coverage=ForecastCoverage(1, 1, 0),
        records=records,
    )
    receipt = ForecastFetchReceipt(
        receipt_id=receipt_id,
        source=SOURCE,
        scheduled_for=persisted_at - timedelta(minutes=5),
        started_at=persisted_at - timedelta(minutes=2),
        response_received_at=persisted_at - timedelta(minutes=1),
        persisted_at=persisted_at,
        outcome=outcome,
        timing=ForecastCaptureTiming.ON_TIME,
        http_status=200,
        payload_hash=payload_hash,
        semantic_hash=semantic_hash,
        revision_id=revision.revision_id,
    )
    return ForecastCaptureWrite(receipt, artifact, revision)


def failed_capture(
    *,
    receipt_id: str = "failed-1",
    persisted_at: datetime = BASE,
) -> ForecastCaptureWrite:
    """Build a failed capture containing no response artifact."""

    return ForecastCaptureWrite(
        ForecastFetchReceipt(
            receipt_id=receipt_id,
            source=SOURCE,
            scheduled_for=persisted_at - timedelta(minutes=5),
            started_at=persisted_at - timedelta(minutes=2),
            persisted_at=persisted_at,
            outcome=ForecastCaptureOutcome.FAILED,
            timing=ForecastCaptureTiming.ON_TIME,
            error_code="transport_error",
        )
    )


def suppressed_capture(
    *,
    receipt_id: str = "suppressed-1",
    persisted_at: datetime = BASE,
) -> ForecastCaptureWrite:
    """Build a suppressed capture containing only scheduling evidence."""

    return ForecastCaptureWrite(
        ForecastFetchReceipt(
            receipt_id=receipt_id,
            source=SOURCE,
            scheduled_for=persisted_at - timedelta(minutes=1),
            persisted_at=persisted_at,
            outcome=ForecastCaptureOutcome.SUPPRESSED,
            timing=ForecastCaptureTiming.ON_TIME,
            error_code="daily_cap",
        )
    )
