"""Verify planned forecast actions become changed, failed, or gap receipts."""

import asyncio
import gzip
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from sleeper_manager.domain.forecast_capture import ForecastCaptureOutcome, ForecastCaptureTiming
from sleeper_manager.integrations.sleeper.forecast_fetch import season_forecast_source
from sleeper_manager.persistence.forecast_sqlite import AsyncSQLiteForecastArchiveRepository
from sleeper_manager.workflows.forecast_capture import execute_forecast_actions
from sleeper_manager.workflows.forecast_schedule import (
    ForecastPlanAction,
    ForecastPlanStep,
    ForecastSlotKind,
    forecast_receipt_id,
)
from tests.paths import FIXTURES_DIR

NOW = datetime(2026, 9, 22, 12, tzinfo=UTC)
SOURCE = season_forecast_source("2026", "regular")
FIXTURE = (FIXTURES_DIR / "sleeper" / "season_forecasts.json").read_bytes()


class RawResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.content = body


def _action(
    scheduled_for: datetime,
    *,
    step: ForecastPlanStep = ForecastPlanStep.FETCH,
    error_code: str | None = None,
) -> ForecastPlanAction:
    """Build one slot action whose receipt id matches the planner."""

    return ForecastPlanAction(
        scheduled_for=scheduled_for,
        kind=ForecastSlotKind.DAILY,
        step=step,
        timing=ForecastCaptureTiming.ON_TIME,
        receipt_id=forecast_receipt_id(SOURCE, scheduled_for),
        error_code=error_code,
    )


def _archive(tmp_path) -> AsyncSQLiteForecastArchiveRepository:  # type: ignore[no-untyped-def]
    """Create an initialized local archive for capture writes."""

    archive = AsyncSQLiteForecastArchiveRepository(tmp_path / "forecasts.db")
    asyncio.run(archive.initialize())
    return archive


def test_valid_feed_records_changed_then_unchanged_without_reparsing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Reuse one raw artifact and revision when the semantic feed is unchanged."""

    archive = _archive(tmp_path)
    seen: list[str] = []

    async def fetch(url: str) -> RawResponse:
        seen.append(url)
        return RawResponse(200, FIXTURE)

    first = asyncio.run(
        execute_forecast_actions(
            archive,
            (_action(NOW - timedelta(minutes=5)),),
            source=SOURCE,
            fetch=fetch,
            now=NOW,
        )
    )
    second = asyncio.run(
        execute_forecast_actions(
            archive,
            (_action(NOW),),
            source=SOURCE,
            fetch=fetch,
            now=NOW,
        )
    )
    stored = asyncio.run(archive.load_artifact(sha256(FIXTURE).hexdigest()))

    assert first[0].outcome is ForecastCaptureOutcome.CHANGED
    assert second[0].outcome is ForecastCaptureOutcome.UNCHANGED
    assert first[0].revision_id == second[0].revision_id
    assert stored is not None
    assert gzip.decompress(stored.encoded_payload) == FIXTURE
    assert len(seen) == 2
    storage = asyncio.run(archive.measure_storage())
    assert (storage.artifact_count, storage.revision_count, storage.receipt_count) == (1, 1, 2)


def test_invalid_http_and_transport_responses_store_gap_evidence(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Keep rejected bodies and failed requests queryable without a revision."""

    archive = _archive(tmp_path)

    async def invalid(url: str) -> RawResponse:
        del url
        return RawResponse(200, b"not-json")

    async def empty(url: str) -> RawResponse:
        del url
        return RawResponse(200, b"")

    async def broken(url: str) -> RawResponse:
        del url
        raise RuntimeError("network down")

    invalid_receipt = asyncio.run(
        execute_forecast_actions(
            archive,
            (_action(NOW - timedelta(minutes=30)),),
            source=SOURCE,
            fetch=invalid,
            now=NOW,
        )
    )[0]
    empty_receipt = asyncio.run(
        execute_forecast_actions(
            archive,
            (_action(NOW - timedelta(minutes=20)),),
            source=SOURCE,
            fetch=empty,
            now=NOW,
        )
    )[0]
    failed_receipt = asyncio.run(
        execute_forecast_actions(
            archive,
            (_action(NOW - timedelta(minutes=10)),),
            source=SOURCE,
            fetch=broken,
            now=NOW,
        )
    )[0]

    assert invalid_receipt.outcome is ForecastCaptureOutcome.INVALID
    assert invalid_receipt.payload_hash == sha256(b"not-json").hexdigest()
    assert invalid_receipt.revision_id is None
    assert empty_receipt.outcome is ForecastCaptureOutcome.FAILED
    assert empty_receipt.error_code == "empty_payload"
    assert empty_receipt.payload_hash is None
    assert failed_receipt.outcome is ForecastCaptureOutcome.FAILED
    assert failed_receipt.error_code == "transport"
    assert failed_receipt.http_status is None
    storage = asyncio.run(archive.measure_storage())
    assert (storage.artifact_count, storage.revision_count, storage.receipt_count) == (1, 0, 3)


def test_suppressed_action_and_exact_retry_do_not_request_the_feed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Record a cap gap once and accept a repeated write of that same receipt."""

    archive = _archive(tmp_path)
    action = _action(
        NOW - timedelta(minutes=5),
        step=ForecastPlanStep.SUPPRESS,
        error_code="daily_cap",
    )

    async def fetch(url: str) -> RawResponse:
        del url
        raise AssertionError("suppressed forecast slot must not fetch")

    first = asyncio.run(
        execute_forecast_actions(archive, (action,), source=SOURCE, fetch=fetch, now=NOW)
    )
    second = asyncio.run(
        execute_forecast_actions(archive, (action,), source=SOURCE, fetch=fetch, now=NOW)
    )

    assert first == second
    assert first[0].outcome is ForecastCaptureOutcome.SUPPRESSED
    assert first[0].started_at is None
    assert asyncio.run(archive.measure_storage()).receipt_count == 1
