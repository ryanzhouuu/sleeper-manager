"""Persist one planned forecast wake without changing lineup advice.

Fetches store a receipt for every outcome, including failures and suppressed
slots. An unchanged semantic forecast reuses the existing revision while still
recording the new raw response.
"""

from __future__ import annotations

import gzip
from collections.abc import Awaitable, Callable
from datetime import datetime
from hashlib import sha256

from sleeper_manager.domain.forecast_capture import (
    ForecastArtifactEncoding,
    ForecastCaptureOutcome,
    ForecastFetchReceipt,
    ForecastSource,
    RawForecastArtifact,
)
from sleeper_manager.integrations.sleeper.forecast_fetch import (
    SleeperForecastFetch,
    SleeperForecastFetchError,
    fetch_season_forecast,
)
from sleeper_manager.integrations.sleeper.forecast_parser import (
    SleeperForecastPayloadError,
    parse_sleeper_season_forecasts,
)
from sleeper_manager.persistence.forecast_repository import (
    AsyncForecastArchiveRepository,
    ForecastArchiveConflictError,
    ForecastCaptureWrite,
)
from sleeper_manager.workflows.forecast_schedule import ForecastPlanAction, ForecastPlanStep


async def execute_forecast_actions(
    archive: AsyncForecastArchiveRepository,
    actions: tuple[ForecastPlanAction, ...],
    *,
    source: ForecastSource,
    fetch: Callable[[str], Awaitable[object]],
    now: datetime,
) -> tuple[ForecastFetchReceipt, ...]:
    """Write every planned action. A fetch uses the supplied raw-response callable."""

    receipts: list[ForecastFetchReceipt] = []
    for action in actions:
        receipts.append(
            await _execute_action(
                archive,
                action,
                source=source,
                fetch=fetch,
                now=now,
            )
        )
    return tuple(receipts)


async def _execute_action(
    archive: AsyncForecastArchiveRepository,
    action: ForecastPlanAction,
    *,
    source: ForecastSource,
    fetch: Callable[[str], Awaitable[object]],
    now: datetime,
) -> ForecastFetchReceipt:
    """Persist one slot and accept an exact retry of the same receipt."""

    if action.step is ForecastPlanStep.SUPPRESS:
        receipt = _suppressed(action, source, now)
        return await _save(archive, ForecastCaptureWrite(receipt))
    try:
        fetched = await fetch_season_forecast(fetch, source=source, clock=lambda: now)
    except SleeperForecastFetchError as error:
        receipt = _failed(action, source, error, now)
        return await _save(archive, ForecastCaptureWrite(receipt))
    if not fetched.body:
        return await _save(archive, ForecastCaptureWrite(_empty(action, source, fetched, now)))
    persisted_at = _persisted_at(now, fetched.received_at, action.scheduled_for)
    try:
        parsed = parse_sleeper_season_forecasts(
            fetched.body,
            source=source,
            persisted_at=persisted_at,
        )
    except SleeperForecastPayloadError:
        artifact = _artifact(fetched.body, persisted_at)
        return await _save(
            archive,
            ForecastCaptureWrite(
                _invalid(action, source, fetched, artifact, persisted_at),
                artifact,
            ),
        )
    existing = await archive.load_revision(parsed.revision.revision_id)
    outcome = (
        ForecastCaptureOutcome.UNCHANGED if existing is not None else ForecastCaptureOutcome.CHANGED
    )
    receipt = _usable(
        action,
        source,
        fetched,
        parsed.revision.revision_id,
        parsed.revision.semantic_hash,
        parsed.artifact.payload_hash,
        persisted_at,
        outcome,
    )
    return await _save(archive, ForecastCaptureWrite(receipt, parsed.artifact, parsed.revision))


async def _save(
    archive: AsyncForecastArchiveRepository,
    capture: ForecastCaptureWrite,
) -> ForecastFetchReceipt:
    """Store a capture, treating an identical receipt retry as success."""

    try:
        await archive.save_capture(capture)
    except ForecastArchiveConflictError:
        stored = await archive.load_receipt(capture.receipt.receipt_id)
        if stored != capture.receipt:
            raise
        return stored
    return capture.receipt


def _suppressed(
    action: ForecastPlanAction,
    source: ForecastSource,
    now: datetime,
) -> ForecastFetchReceipt:
    """Record a cap gap that never opened a request."""

    if action.error_code is None:
        raise ValueError("Suppressed forecast action is missing an error code")
    return ForecastFetchReceipt(
        receipt_id=action.receipt_id,
        source=source,
        scheduled_for=action.scheduled_for,
        persisted_at=_persisted_at(now, None, action.scheduled_for),
        outcome=ForecastCaptureOutcome.SUPPRESSED,
        timing=action.timing,
        error_code=action.error_code,
    )


def _failed(
    action: ForecastPlanAction,
    source: ForecastSource,
    error: SleeperForecastFetchError,
    now: datetime,
) -> ForecastFetchReceipt:
    """Record a request that produced no usable payload."""

    return ForecastFetchReceipt(
        receipt_id=action.receipt_id,
        source=source,
        scheduled_for=action.scheduled_for,
        started_at=error.started_at,
        response_received_at=error.received_at,
        persisted_at=_persisted_at(now, error.received_at, action.scheduled_for, error.started_at),
        outcome=ForecastCaptureOutcome.FAILED,
        timing=action.timing,
        http_status=error.status,
        error_code=error.code,
    )


def _empty(
    action: ForecastPlanAction,
    source: ForecastSource,
    fetched: SleeperForecastFetch,
    now: datetime,
) -> ForecastFetchReceipt:
    """Record an empty success body, which cannot be stored as an artifact."""

    return ForecastFetchReceipt(
        receipt_id=action.receipt_id,
        source=source,
        scheduled_for=action.scheduled_for,
        started_at=fetched.started_at,
        response_received_at=fetched.received_at,
        persisted_at=_persisted_at(now, fetched.received_at, action.scheduled_for),
        outcome=ForecastCaptureOutcome.FAILED,
        timing=action.timing,
        http_status=fetched.status,
        error_code="empty_payload",
    )


def _invalid(
    action: ForecastPlanAction,
    source: ForecastSource,
    fetched: SleeperForecastFetch,
    artifact: RawForecastArtifact,
    persisted_at: datetime,
) -> ForecastFetchReceipt:
    """Record a successful response that the season parser rejected."""

    return ForecastFetchReceipt(
        receipt_id=action.receipt_id,
        source=source,
        scheduled_for=action.scheduled_for,
        started_at=fetched.started_at,
        response_received_at=fetched.received_at,
        persisted_at=persisted_at,
        outcome=ForecastCaptureOutcome.INVALID,
        timing=action.timing,
        http_status=fetched.status,
        payload_hash=artifact.payload_hash,
        error_code="invalid_payload",
    )


def _usable(
    action: ForecastPlanAction,
    source: ForecastSource,
    fetched: SleeperForecastFetch,
    revision_id: str,
    semantic_hash: str,
    payload_hash: str,
    persisted_at: datetime,
    outcome: ForecastCaptureOutcome,
) -> ForecastFetchReceipt:
    """Record a parsed forecast as changed or unchanged."""

    return ForecastFetchReceipt(
        receipt_id=action.receipt_id,
        source=source,
        scheduled_for=action.scheduled_for,
        started_at=fetched.started_at,
        response_received_at=fetched.received_at,
        persisted_at=persisted_at,
        outcome=outcome,
        timing=action.timing,
        http_status=fetched.status,
        payload_hash=payload_hash,
        semantic_hash=semantic_hash,
        revision_id=revision_id,
    )


def _artifact(payload: bytes, stored_at: datetime) -> RawForecastArtifact:
    """Gzip the original bytes with the same settings as the season parser."""

    return RawForecastArtifact(
        payload_hash=sha256(payload).hexdigest(),
        encoding=ForecastArtifactEncoding.GZIP_JSON,
        encoded_payload=gzip.compress(payload, compresslevel=6, mtime=0),
        uncompressed_size=len(payload),
        stored_at=stored_at,
    )


def _persisted_at(
    now: datetime,
    received_at: datetime | None,
    scheduled_for: datetime,
    started_at: datetime | None = None,
) -> datetime:
    """Keep persistence at or after the request, response, and slot time."""

    persisted = now
    for candidate in (received_at, scheduled_for, started_at):
        if candidate is not None and candidate > persisted:
            persisted = candidate
    return persisted


__all__ = ("execute_forecast_actions",)
