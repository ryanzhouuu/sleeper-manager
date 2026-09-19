"""Contract tests for immutable, cutoff-safe forecast evidence."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.domain.forecast_capture import (
    ForecastArtifactEncoding,
    ForecastCaptureError,
    ForecastCaptureOutcome,
    ForecastCaptureTiming,
    ForecastCoverage,
    ForecastFetchReceipt,
    ForecastRetrievalResult,
    ForecastRetrievalStatus,
    ForecastRevisionProvenance,
    ForecastSource,
    NormalizedForecastRevision,
    NormalizedPlayerForecast,
    RawForecastArtifact,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
AT = datetime(2026, 9, 19, 12, tzinfo=UTC)


def source() -> ForecastSource:
    """Build the qualified season-feed identity used by contract tests."""

    return ForecastSource(
        provider="sleeper",
        endpoint="/projections/nba/2026?season_type=regular",
        season="2026",
        season_type="regular",
        horizon="season",
        adapter_version="sleeper-season-forecast-v1",
    )


def player(player_id: str = "1000") -> NormalizedPlayerForecast:
    """Build one normalized row while retaining undocumented numeric fields."""

    return NormalizedPlayerForecast(
        player_id=player_id,
        company="rotowire",
        team_id="GSW",
        provider_updated_at=AT - timedelta(hours=1),
        stats=(("sp", 1680), ("pts", 15.77), ("ast", 4.2)),
    )


def provenance(*, persisted_at: datetime = AT) -> ForecastRevisionProvenance:
    """Build provenance whose persistence time controls cutoff visibility."""

    return ForecastRevisionProvenance(
        receipt_id="receipt-1",
        revision_id="revision-1",
        source=source(),
        payload_hash=HASH_A,
        semantic_hash=HASH_B,
        persisted_at=persisted_at,
    )


def test_normalized_player_forecast_preserves_absence_and_orders_numeric_stats() -> None:
    forecast = player()

    assert forecast.stats == (("ast", 4.2), ("pts", 15.77), ("sp", 1680.0))
    assert forecast.stat("pts") == 15.77
    assert forecast.stat("minutes") is None
    with pytest.raises(FrozenInstanceError):
        forecast.team_id = "MIA"  # type: ignore[misc]


@pytest.mark.parametrize(
    "stats",
    (
        (("pts", 10), ("pts", 11)),
        (("pts", float("nan")),),
        (("", 10),),
        (("pts", True),),
    ),
)
def test_normalized_player_forecast_rejects_ambiguous_stats(
    stats: tuple[tuple[str, float], ...],
) -> None:
    with pytest.raises(ForecastCaptureError):
        NormalizedPlayerForecast(player_id="1000", company="rotowire", stats=stats)


def test_raw_artifact_and_revision_keep_raw_and_semantic_identities_separate() -> None:
    artifact = RawForecastArtifact(
        payload_hash=HASH_A,
        encoding=ForecastArtifactEncoding.GZIP_JSON,
        encoded_payload=b"compressed-json",
        uncompressed_size=100,
        stored_at=AT,
    )
    revision = NormalizedForecastRevision(
        revision_id="revision-1",
        source=source(),
        semantic_hash=HASH_B,
        source_payload_hash=artifact.payload_hash,
        first_persisted_at=AT,
        coverage=ForecastCoverage(total_rows=2, numeric_forecast_rows=1, core_complete_rows=1),
        records=(player("2000"), player("1000")),
        provider_updated_from=AT - timedelta(hours=2),
        provider_updated_to=AT - timedelta(hours=1),
    )

    assert revision.source_payload_hash == artifact.payload_hash
    assert tuple(item.player_id for item in revision.records) == ("1000", "2000")


def test_revision_rejects_duplicate_players_and_inconsistent_coverage() -> None:
    with pytest.raises(ForecastCaptureError, match="duplicate player"):
        NormalizedForecastRevision(
            revision_id="revision-1",
            source=source(),
            semantic_hash=HASH_B,
            source_payload_hash=HASH_A,
            first_persisted_at=AT,
            coverage=ForecastCoverage(2, 2, 2),
            records=(player(), player()),
        )
    with pytest.raises(ForecastCaptureError, match="coverage total"):
        NormalizedForecastRevision(
            revision_id="revision-1",
            source=source(),
            semantic_hash=HASH_B,
            source_payload_hash=HASH_A,
            first_persisted_at=AT,
            coverage=ForecastCoverage(2, 1, 1),
            records=(player(),),
        )


def test_changed_receipt_requires_complete_usable_evidence() -> None:
    receipt = ForecastFetchReceipt(
        receipt_id="receipt-1",
        source=source(),
        scheduled_for=AT,
        started_at=AT + timedelta(minutes=1),
        response_received_at=AT + timedelta(minutes=1, seconds=2),
        persisted_at=AT + timedelta(minutes=1, seconds=3),
        outcome=ForecastCaptureOutcome.CHANGED,
        timing=ForecastCaptureTiming.ON_TIME,
        http_status=200,
        payload_hash=HASH_A,
        semantic_hash=HASH_B,
        revision_id="revision-1",
    )

    assert receipt.error_code is None


def test_metadata_only_receipt_can_reuse_a_semantic_revision() -> None:
    receipt = ForecastFetchReceipt(
        receipt_id="receipt-2",
        source=source(),
        scheduled_for=AT,
        started_at=AT,
        response_received_at=AT + timedelta(seconds=1),
        persisted_at=AT + timedelta(seconds=2),
        outcome=ForecastCaptureOutcome.UNCHANGED,
        timing=ForecastCaptureTiming.ON_TIME,
        http_status=200,
        payload_hash=HASH_B,
        semantic_hash=HASH_A,
        revision_id="existing-revision",
    )

    assert receipt.payload_hash != receipt.semantic_hash
    assert receipt.revision_id == "existing-revision"


@pytest.mark.parametrize(
    "receipt",
    (
        ForecastFetchReceipt(
            receipt_id="failed",
            source=source(),
            scheduled_for=AT,
            started_at=AT,
            persisted_at=AT + timedelta(seconds=1),
            outcome=ForecastCaptureOutcome.FAILED,
            timing=ForecastCaptureTiming.ON_TIME,
            error_code="network_error",
        ),
        ForecastFetchReceipt(
            receipt_id="invalid",
            source=source(),
            scheduled_for=AT,
            started_at=AT,
            response_received_at=AT + timedelta(seconds=1),
            persisted_at=AT + timedelta(seconds=2),
            outcome=ForecastCaptureOutcome.INVALID,
            timing=ForecastCaptureTiming.ON_TIME,
            http_status=200,
            payload_hash=HASH_A,
            error_code="invalid_schema",
        ),
        ForecastFetchReceipt(
            receipt_id="suppressed",
            source=source(),
            scheduled_for=AT,
            persisted_at=AT,
            outcome=ForecastCaptureOutcome.SUPPRESSED,
            timing=ForecastCaptureTiming.ON_TIME,
            error_code="daily_cap",
        ),
    ),
)
def test_gap_receipts_preserve_failure_evidence(receipt: ForecastFetchReceipt) -> None:
    assert receipt.error_code is not None
    assert receipt.revision_id is None


def test_receipt_rejects_evidence_that_conflicts_with_its_outcome() -> None:
    with pytest.raises(ForecastCaptureError, match="Usable forecast"):
        ForecastFetchReceipt(
            receipt_id="receipt-1",
            source=source(),
            scheduled_for=AT,
            started_at=AT,
            response_received_at=AT + timedelta(seconds=1),
            persisted_at=AT + timedelta(seconds=2),
            outcome=ForecastCaptureOutcome.CHANGED,
            timing=ForecastCaptureTiming.ON_TIME,
            http_status=200,
            payload_hash=HASH_A,
        )
    with pytest.raises(ForecastCaptureError, match="Suppressed forecast"):
        ForecastFetchReceipt(
            receipt_id="receipt-2",
            source=source(),
            scheduled_for=AT,
            started_at=AT,
            persisted_at=AT,
            outcome=ForecastCaptureOutcome.SUPPRESSED,
            timing=ForecastCaptureTiming.ON_TIME,
            error_code="daily_cap",
        )


def test_available_retrieval_reports_cutoff_age() -> None:
    result = ForecastRetrievalResult(
        status=ForecastRetrievalStatus.AVAILABLE,
        player_id="1000",
        cutoff=AT + timedelta(hours=2),
        detail="latest revision before cutoff",
        forecast=player(),
        provenance=provenance(),
    )

    assert result.age == timedelta(hours=2)


def test_retrieval_rejects_post_cutoff_and_mismatched_player_evidence() -> None:
    with pytest.raises(ForecastCaptureError, match="post-cutoff"):
        ForecastRetrievalResult(
            status=ForecastRetrievalStatus.AVAILABLE,
            player_id="1000",
            cutoff=AT,
            detail="future evidence",
            forecast=player(),
            provenance=provenance(persisted_at=AT + timedelta(seconds=1)),
        )
    with pytest.raises(ForecastCaptureError, match="different player"):
        ForecastRetrievalResult(
            status=ForecastRetrievalStatus.AVAILABLE,
            player_id="other",
            cutoff=AT,
            detail="wrong player",
            forecast=player(),
            provenance=provenance(),
        )


def test_missing_and_gap_retrievals_preserve_distinct_evidence() -> None:
    missing = ForecastRetrievalResult(
        status=ForecastRetrievalStatus.MISSING,
        player_id="missing-player",
        cutoff=AT,
        detail="player has no numeric source row",
        provenance=provenance(),
    )
    gap = ForecastRetrievalResult(
        status=ForecastRetrievalStatus.GAP,
        player_id="1000",
        cutoff=AT,
        detail="pre-tipoff capture failed",
        evidence_receipt_id="failed-receipt",
    )

    assert missing.age == timedelta(0)
    assert gap.age is None
