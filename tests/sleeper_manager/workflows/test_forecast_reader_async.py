"""D1 batch reads preserve the same cutoff, gap, and integrity semantics as SQLite."""

import asyncio
from datetime import timedelta

import pytest

from sleeper_manager.domain.forecast_capture import ForecastRetrievalStatus
from sleeper_manager.persistence.forecast_repository import ForecastArchiveIntegrityError
from sleeper_manager.workflows.forecast_reader import read_player_forecasts_async
from tests.sleeper_manager.persistence.forecast_sqlite_support import (
    BASE,
    SOURCE,
    failed_capture,
    successful_capture,
)
from tests.sleeper_manager.persistence.test_forecast_d1_reads import repository


def test_empty_d1_archive_produces_no_invented_receipt() -> None:
    _, archive = repository()
    assert asyncio.run(read_player_forecasts_async(archive, SOURCE, ("p",), BASE)) == ()


def test_batch_uses_latest_revision_and_retains_newer_failure() -> None:
    _, archive = repository()
    for capture in (
        successful_capture(),
        successful_capture(
            receipt_id="removed",
            persisted_at=BASE + timedelta(hours=1),
            payload=b"new",
            player_id="player-2",
        ),
        failed_capture(persisted_at=BASE + timedelta(hours=2)),
    ):
        asyncio.run(archive.save_capture(capture))
    result = asyncio.run(
        read_player_forecasts_async(
            archive, SOURCE, ("player-1", "player-2"), BASE + timedelta(hours=2)
        )
    )
    assert result[0].status is ForecastRetrievalStatus.MISSING
    assert result[1].status is ForecastRetrievalStatus.AVAILABLE
    assert all(row.newer_attempt_receipt_id == "failed-1" for row in result)
    assert all(row.provenance and row.provenance.receipt_id == "removed" for row in result)


def test_corrupt_d1_revision_cannot_be_relabelled_missing() -> None:
    database, archive = repository()
    asyncio.run(archive.save_capture(successful_capture()))
    database.connection.execute("UPDATE forecast_revisions SET encoded_records = 'corrupt'")
    with pytest.raises(ForecastArchiveIntegrityError):
        asyncio.run(read_player_forecasts_async(archive, SOURCE, ("player-1",), BASE))
