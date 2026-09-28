"""Select one player's forecast from evidence persisted by a decision cutoff.

The newest usable revision wins. A later failed, invalid, or suppressed attempt
stays attached to that result. A revision that drops a player does not fall
back to an older forecast. This reader does not decide whether the forecast
is fit for advice.
"""

from __future__ import annotations

from datetime import datetime

from sleeper_manager.domain.forecast_capture import (
    ForecastCaptureError,
    ForecastCaptureOutcome,
    ForecastFetchReceipt,
    ForecastRetrievalResult,
    ForecastRetrievalStatus,
    ForecastRevisionProvenance,
    ForecastSource,
)
from sleeper_manager.persistence.forecast_repository import (
    ForecastArchiveRepository,
    ForecastArchiveSelection,
)


def read_player_forecasts(
    archive: ForecastArchiveRepository,
    source: ForecastSource,
    player_ids: tuple[str, ...],
    cutoff: datetime,
) -> tuple[ForecastRetrievalResult, ...]:
    """Load one cutoff's revision once, then select each requested player."""

    selection = archive.load_revision_at_cutoff(source, cutoff=cutoff)
    latest = archive.load_latest_receipt(source, cutoff=cutoff)
    if selection is None and latest is None:
        raise ForecastCaptureError("No forecast receipt is visible at the cutoff")
    return tuple(_select_player(player_id, cutoff, selection, latest) for player_id in player_ids)


def _select_player(
    player_id: str,
    cutoff: datetime,
    selection: ForecastArchiveSelection | None,
    latest: ForecastFetchReceipt | None,
) -> ForecastRetrievalResult:
    """Return one player's cutoff result from an already loaded revision."""

    newer_attempt = _newer_attempt(selection, latest)
    if selection is None:
        return _without_revision(player_id, cutoff, latest)
    forecast = next(
        (record for record in selection.revision.records if record.player_id == player_id),
        None,
    )
    provenance = _provenance(selection)
    if forecast is None:
        return ForecastRetrievalResult(
            status=ForecastRetrievalStatus.MISSING,
            player_id=player_id,
            cutoff=cutoff,
            detail="player absent from revision",
            provenance=provenance,
            coverage=selection.revision.coverage,
            newer_attempt_receipt_id=newer_attempt,
        )
    return ForecastRetrievalResult(
        status=ForecastRetrievalStatus.AVAILABLE,
        player_id=player_id,
        cutoff=cutoff,
        detail="revision available at cutoff",
        forecast=forecast,
        provenance=provenance,
        coverage=selection.revision.coverage,
        newer_attempt_receipt_id=newer_attempt,
    )


def _without_revision(
    player_id: str,
    cutoff: datetime,
    latest: ForecastFetchReceipt | None,
) -> ForecastRetrievalResult:
    """Describe the newest attempt when no usable revision exists by the cutoff."""

    if latest is None:
        raise ForecastCaptureError("No forecast receipt is visible at the cutoff")
    if latest.outcome is ForecastCaptureOutcome.INVALID:
        status = ForecastRetrievalStatus.INVALID
    elif latest.outcome in (ForecastCaptureOutcome.FAILED, ForecastCaptureOutcome.SUPPRESSED):
        status = ForecastRetrievalStatus.GAP
    else:
        raise ForecastCaptureError("Forecast cutoff receipt is missing its revision")
    error_code = latest.error_code or latest.outcome.value
    return ForecastRetrievalResult(
        status=status,
        player_id=player_id,
        cutoff=cutoff,
        detail=f"{latest.outcome.value}:{error_code}",
        evidence_receipt_id=latest.receipt_id,
    )


def _newer_attempt(
    selection: ForecastArchiveSelection | None,
    latest: ForecastFetchReceipt | None,
) -> str | None:
    """Attach a failed attempt only when it is newer than the selected receipt."""

    if selection is None or latest is None:
        return None
    if latest.outcome not in (
        ForecastCaptureOutcome.FAILED,
        ForecastCaptureOutcome.INVALID,
        ForecastCaptureOutcome.SUPPRESSED,
    ):
        return None
    selected = selection.receipt
    if (latest.persisted_at, latest.receipt_id) <= (selected.persisted_at, selected.receipt_id):
        return None
    return latest.receipt_id


def _provenance(selection: ForecastArchiveSelection) -> ForecastRevisionProvenance:
    """Bind the selected receipt's persistence time to its revision."""

    receipt = selection.receipt
    if receipt.payload_hash is None or receipt.semantic_hash is None or receipt.revision_id is None:
        raise ForecastCaptureError("Usable forecast receipt is missing revision provenance")
    return ForecastRevisionProvenance(
        receipt_id=receipt.receipt_id,
        revision_id=receipt.revision_id,
        source=receipt.source,
        payload_hash=receipt.payload_hash,
        semantic_hash=receipt.semantic_hash,
        persisted_at=receipt.persisted_at,
    )


__all__ = ("read_player_forecasts",)
