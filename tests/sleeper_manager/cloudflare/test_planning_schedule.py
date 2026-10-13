"""A pre-tipoff refresh must leave time before the configured movement deadline."""

import asyncio
from datetime import timedelta

from sleeper_manager.cloudflare.planning_schedule import _replace_future_pre_tipoff
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import DueWorkKind
from tests.sleeper_manager.cloudflare.test_scheduled_dispatcher import NOW, POLICY, _assembly


def test_pre_tipoff_wake_precedes_move_deadline_by_one_cron_interval(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        inputs = _assembly().evidence.inputs
        await _replace_future_pre_tipoff(repository, POLICY, inputs, NOW)
        work = await repository.list_scheduled_work(kind=DueWorkKind.PRE_TIPOFF)
        starts = {
            game.provider_id: game.start_time
            for result in inputs.schedule_results
            for game in result.games
        }
        assert work
        for row in work:
            movement_deadline = starts[row.game_id] - POLICY.move_lead_time
            assert row.due_at == movement_deadline - timedelta(minutes=5)
            assert row.due_at < movement_deadline

    asyncio.run(exercise())
