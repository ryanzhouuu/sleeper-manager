"""A pre-tipoff refresh must leave time before the configured movement deadline."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from sleeper_manager.cloudflare.planning_schedule import _replace_future_pre_tipoff
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import DueWorkKind, ScheduledWorkStatus
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


def test_pre_tipoff_watches_are_limited_to_half_open_fantasy_week(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        inputs = _assembly().evidence.inputs
        week = inputs.week_window
        template = inputs.schedule_results[0]
        games = tuple(
            replace(template.games[0], provider_id=name, start_time=at)
            for name, at in (
                ("before", week.starts_at - timedelta(seconds=1)),
                ("first", week.starts_at),
                ("last", week.ends_at - timedelta(seconds=1)),
                ("after", week.ends_at),
            )
        )
        inputs = replace(inputs, schedule_results=(replace(template, games=games),))
        await _replace_future_pre_tipoff(
            repository, POLICY, inputs, week.starts_at - timedelta(minutes=1)
        )
        work = await repository.list_scheduled_work(kind=DueWorkKind.PRE_TIPOFF)
        assert {row.game_id for row in work} == {"first", "last"}

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "status",
    [
        ScheduledWorkStatus.PENDING,
        ScheduledWorkStatus.RETRY,
        ScheduledWorkStatus.RUNNING,
        ScheduledWorkStatus.COMPLETED,
    ],
)
def test_unchanged_watches_do_not_write_or_reset_retry_times(tmp_path, status) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        inputs = _assembly().evidence.inputs
        await _replace_future_pre_tipoff(repository, POLICY, inputs, NOW)
        for work in await repository.list_scheduled_work(kind=DueWorkKind.PRE_TIPOFF):
            await repository.upsert_scheduled_work(
                replace(
                    work,
                    status=status,
                    due_at=work.due_at
                    + (
                        timedelta(minutes=5)
                        if status is ScheduledWorkStatus.RETRY
                        else timedelta(0)
                    ),
                )
            )
        before = await repository.list_scheduled_work()
        with patch.object(
            repository, "upsert_scheduled_work", wraps=repository.upsert_scheduled_work
        ) as write:
            await _replace_future_pre_tipoff(repository, POLICY, inputs, NOW + timedelta(minutes=1))
        assert write.call_count == 0
        assert await repository.list_scheduled_work() == before

    asyncio.run(exercise())


def test_rescheduled_game_updates_the_watch(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        inputs = _assembly().evidence.inputs
        await _replace_future_pre_tipoff(repository, POLICY, inputs, NOW)
        schedule = inputs.schedule_results[0]
        games = tuple(
            replace(game, start_time=game.start_time + timedelta(hours=1))
            for game in schedule.games
        )
        changed = replace(inputs, schedule_results=(replace(schedule, games=games),))
        await _replace_future_pre_tipoff(repository, POLICY, changed, NOW)
        starts = {game.provider_id: game.start_time for game in games}
        for work in await repository.list_scheduled_work(kind=DueWorkKind.PRE_TIPOFF):
            assert work.deadline == starts[work.game_id]
            assert work.due_at == starts[work.game_id] - POLICY.move_lead_time - timedelta(
                minutes=5
            )

    asyncio.run(exercise())


def test_obsolete_future_watches_are_canceled_in_bounded_groups(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        inputs = _assembly().evidence.inputs
        await _replace_future_pre_tipoff(repository, POLICY, inputs, NOW)
        template = (await repository.list_scheduled_work(kind=DueWorkKind.PRE_TIPOFF))[0]
        for index in range(70):
            await repository.upsert_scheduled_work(
                replace(
                    template,
                    work_id=f"obsolete-{index}",
                    dedupe_key=f"pre_tipoff:old-{index}",
                    game_id=f"old-{index}",
                    deadline=inputs.week_window.ends_at + timedelta(days=1),
                    due_at=inputs.week_window.ends_at,
                )
            )
        write = AsyncMock(wraps=repository.upsert_scheduled_work)
        with patch.object(repository, "upsert_scheduled_work", write):
            await _replace_future_pre_tipoff(repository, POLICY, inputs, NOW)
        assert write.await_count == 50
        canceled = await repository.list_scheduled_work(statuses=(ScheduledWorkStatus.CANCELED,))
        assert len(canceled) == 50
        await _replace_future_pre_tipoff(repository, POLICY, inputs, NOW)
        canceled = await repository.list_scheduled_work(statuses=(ScheduledWorkStatus.CANCELED,))
        assert len(canceled) == 70

    asyncio.run(exercise())
