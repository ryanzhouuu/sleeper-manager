import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.cloudflare.projection_history import D1ProjectionHistory
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import ProjectionObservationRecord
from sleeper_manager.projections.live_baseline import LiveProjectionTarget, ProjectionHistoryError

NOW = datetime(2026, 1, 7, 18, tzinfo=UTC)


def _record(box_score_json: str = '{"points":20}') -> ProjectionObservationRecord:
    return ProjectionObservationRecord(
        history_version="history-v1",
        player_id="401",
        game_id="old-game",
        game_start=NOW - timedelta(days=2),
        outcome_finalized_at=NOW - timedelta(hours=44),
        minutes=30,
        started=True,
        did_not_play=False,
        box_score_json=box_score_json,
        source_version="espn-v1",
    )


def test_d1_history_loads_compact_observations(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> D1ProjectionHistory:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        later = replace(
            _record(),
            player_id="402",
            game_id="older-game",
            game_start=NOW - timedelta(days=3),
            outcome_finalized_at=NOW - timedelta(days=2),
        )
        await repository.save_projection_observations((_record(), later))
        paged = await repository.load_projection_observations("history-v1", page_size=1)
        assert [row.player_id for row in paged] == ["402", "401"]
        return await D1ProjectionHistory.load_version(
            repository,
            history_version="history-v1",
            loaded_at=NOW,
        )

    history = asyncio.run(exercise())
    result = history.load(
        LiveProjectionTarget("p1", "next-game", NOW + timedelta(hours=3), "401"),
        before=NOW,
    )

    assert result.dataset_version == "history-v1"
    assert len(result.rows) == 2
    observation = next(row for row in result.rows if row.player_id == "401")
    assert observation.player_id == "401"
    assert observation.box_score.points == 20


def test_d1_history_rejects_invalid_box_score_shape(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        await repository.save_projection_observations((_record(json.dumps({"points": "twenty"})),))
        await D1ProjectionHistory.load_version(
            repository,
            history_version="history-v1",
            loaded_at=NOW,
        )

    with pytest.raises(ProjectionHistoryError, match="must be an integer"):
        asyncio.run(exercise())


def test_d1_history_rejects_empty_version(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        await D1ProjectionHistory.load_version(
            repository,
            history_version="missing-history-v1",
            loaded_at=NOW,
        )

    with pytest.raises(ProjectionHistoryError, match="no observations"):
        asyncio.run(exercise())
