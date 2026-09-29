"""Verify immutable companion snapshots in the local forecast archive."""

from dataclasses import replace

import pytest

from sleeper_manager.domain.forecast_context import (
    ForecastContextGap,
    ForecastContextGapCode,
)
from sleeper_manager.persistence.forecast_repository import ForecastArchiveConflictError
from sleeper_manager.persistence.forecast_sqlite import SQLiteForecastArchiveRepository
from tests.sleeper_manager.domain.test_forecast_context import AT, context
from tests.sleeper_manager.persistence.forecast_sqlite_support import (
    failed_capture,
    successful_capture,
)


def repository(tmp_path) -> SQLiteForecastArchiveRepository:  # type: ignore[no-untyped-def]
    """Create one initialized file-backed forecast archive."""

    archive = SQLiteForecastArchiveRepository(tmp_path / "forecasts.db")
    archive.initialize()
    return archive


def test_context_write_is_immutable_and_exact_retry_reloads_it(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Store one snapshot with its receipt and accept only the same bytes again."""

    archive = repository(tmp_path)
    capture = replace(successful_capture(persisted_at=AT), context=context())

    created = archive.save_capture(capture)
    retried = archive.save_capture(capture)

    assert created.context_created is True
    assert retried.context_created is False
    assert archive.load_context("receipt-1") == capture.context
    with pytest.raises(ForecastArchiveConflictError, match="companion evidence"):
        archive.save_capture(
            replace(
                capture,
                context=context(
                    gaps=(
                        ForecastContextGap(
                            ForecastContextGapCode.IDENTITY,
                            "1002",
                            "player is unmapped",
                        ),
                    )
                ),
            )
        )
    assert archive.load_context("receipt-1") == capture.context


def test_failed_capture_keeps_an_explicit_companion_gap(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A failed forecast still preserves the companion gap recorded with it."""

    archive = repository(tmp_path)
    gap = context(
        receipt_id="failed-1",
        matchup_id=None,
        manager=None,
        opponent=None,
        eligibility=(),
        games=(),
        gaps=(
            ForecastContextGap(ForecastContextGapCode.MATCHUP, "league-1", "matchup read failed"),
            ForecastContextGap(ForecastContextGapCode.ROSTER, "manager", "roster read failed"),
        ),
    )
    capture = replace(failed_capture(persisted_at=AT), context=gap)

    result = archive.save_capture(capture)

    assert result.context_created is True
    assert archive.load_context("failed-1") == gap


def test_receipt_without_context_stays_without_one(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Existing receipts are not backfilled when a later write adds a snapshot."""

    archive = repository(tmp_path)
    capture = successful_capture(persisted_at=AT)
    archive.save_capture(capture)

    assert archive.load_context("receipt-1") is None
    with pytest.raises(ForecastArchiveConflictError, match="companion evidence"):
        archive.save_capture(replace(capture, context=context()))
