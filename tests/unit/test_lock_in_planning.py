"""Opportunity creation and pre-tipoff evidence capture coverage."""

import asyncio
from dataclasses import replace
from datetime import timedelta

from test_planning_inputs import NOW, _game, _inputs, _profile, _schedule_result

from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.domain.nba import GameStatus
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import DueWorkKind
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityKey
from sleeper_manager.workflows.lock_in_planning import sync_live_lock_in_opportunities


def test_planning_creates_player_opportunities_and_one_watch_per_game(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Coalesce roster players into one game-level ESPN summary watch."""

    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        first = _game("g1", start=NOW + timedelta(hours=2))
        second = _game("g2", start=NOW + timedelta(days=1))
        inputs = _inputs(schedule_results=(_schedule_result(first, second),))

        await sync_live_lock_in_opportunities(
            inputs,
            repository=repository,
            observed_at=NOW,
        )

        p1 = await repository.get_lock_in_opportunity(
            LockInOpportunityKey("league-1", 1, 1, "p1", "g1")
        )
        p2 = await repository.get_lock_in_opportunity(
            LockInOpportunityKey("league-1", 1, 1, "p2", "g1")
        )
        work = await repository.list_scheduled_work(kind=DueWorkKind.POSTGAME)
        assert p1 is not None and p1.slot_index == 0 and p1.slot_position == "PG"
        assert p2 is not None and p2.slot_index == 1 and p2.slot_position == "UTIL"
        assert p1.roster_evidence_at == inputs.league_profile.retrieved_at
        assert p1.action_deadline == second.start_time - inputs.move_lead_time
        assert len(work) == 2
        assert {item.game_id for item in work} == {"g1", "g2"}
        assert next(item for item in work if item.game_id == "g1").due_at == (
            first.start_time + timedelta(hours=2)
        )

    asyncio.run(exercise())


def test_post_tipoff_sync_preserves_last_safe_starter_snapshot(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Never replace durable tipoff evidence with a later Sleeper roster view."""

    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        game = _game("g1", start=NOW + timedelta(hours=2))
        before = _inputs(
            _profile(starter_ids=("p1", "p2")),
            schedule_results=(_schedule_result(game),),
        )
        await sync_live_lock_in_opportunities(
            before,
            repository=repository,
            observed_at=NOW,
        )
        after_profile = replace(
            _profile(
                starter_ids=("p2", "p1"),
                retrieved_at=game.start_time + timedelta(minutes=1),
            )
        )
        after = replace(before, league_profile=after_profile)

        await sync_live_lock_in_opportunities(
            after,
            repository=repository,
            observed_at=game.start_time + timedelta(minutes=1),
        )

        stored = await repository.get_lock_in_opportunity(
            LockInOpportunityKey("league-1", 1, 1, "p1", "g1")
        )
        assert stored is not None
        assert stored.slot_index == 0
        assert stored.slot_position == "PG"
        assert stored.roster_evidence_at == before.league_profile.retrieved_at

    asyncio.run(exercise())


def test_latest_pre_tipoff_nonstarter_snapshot_replaces_stale_slot(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Use Sleeper retrieval time when collection finishes after tipoff."""

    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        game = _game("g1", start=NOW + timedelta(hours=2))
        before = _inputs(
            _profile(starter_ids=("p1", "p2")),
            schedule_results=(_schedule_result(game),),
        )
        await sync_live_lock_in_opportunities(
            before,
            repository=repository,
            observed_at=NOW,
        )
        latest_profile = _profile(
            starter_ids=(None, "p2"),
            retrieved_at=game.start_time - timedelta(minutes=1),
        )

        await sync_live_lock_in_opportunities(
            replace(before, league_profile=latest_profile),
            repository=repository,
            observed_at=game.start_time + timedelta(minutes=1),
        )

        stored = await repository.get_lock_in_opportunity(
            LockInOpportunityKey("league-1", 1, 1, "p1", "g1")
        )
        assert stored is not None
        assert stored.status is LockInOpportunityStatus.INELIGIBLE
        assert stored.rostered_at_tipoff is False
        assert stored.slot_index is None
        assert stored.slot_position is None
        assert stored.roster_evidence_at == latest_profile.retrieved_at

    asyncio.run(exercise())


def test_missing_starter_or_conflicting_eligibility_suppresses_action(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Mark unsafe pre-tipoff evidence ineligible instead of inventing a slot."""

    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        game = _game("g1", start=NOW + timedelta(hours=2))
        inputs = _inputs(
            _profile(starter_ids=(None, "p2")),
            schedule_results=(_schedule_result(game),),
        )

        await sync_live_lock_in_opportunities(
            inputs,
            repository=repository,
            observed_at=NOW,
        )

        stored = await repository.get_lock_in_opportunity(
            LockInOpportunityKey("league-1", 1, 1, "p1", "g1")
        )
        assert stored is not None
        assert stored.status is LockInOpportunityStatus.INELIGIBLE
        assert stored.rostered_at_tipoff is False
        assert stored.slot_index is None

    asyncio.run(exercise())


def test_postponed_or_canceled_games_require_reconciliation(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Unsafe schedule status must not be treated as a lockable player-game."""

    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        postponed = _game("g1", start=NOW + timedelta(hours=2), status=GameStatus.POSTPONED)
        canceled = _game("g2", start=NOW + timedelta(days=1), status=GameStatus.CANCELED)
        inputs = _inputs(schedule_results=(_schedule_result(postponed, canceled),))

        await sync_live_lock_in_opportunities(
            inputs,
            repository=repository,
            observed_at=NOW,
        )

        first = await repository.get_lock_in_opportunity(
            LockInOpportunityKey("league-1", 1, 1, "p1", "g1")
        )
        second = await repository.get_lock_in_opportunity(
            LockInOpportunityKey("league-1", 1, 1, "p1", "g2")
        )
        assert first is not None
        assert second is not None
        assert first.status is LockInOpportunityStatus.RECONCILIATION_REQUIRED
        assert second.status is LockInOpportunityStatus.RECONCILIATION_REQUIRED

    asyncio.run(exercise())


def test_schedule_change_refreshes_start_and_deadline(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Daily planning must follow a moved tipoff while the watch is still open."""

    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        original = _game("g1", start=NOW + timedelta(hours=2))
        later = _game("g2", start=NOW + timedelta(days=1))
        inputs = _inputs(schedule_results=(_schedule_result(original, later),))
        await sync_live_lock_in_opportunities(
            inputs,
            repository=repository,
            observed_at=NOW,
        )
        moved = replace(original, start_time=NOW + timedelta(hours=5))
        await sync_live_lock_in_opportunities(
            replace(inputs, schedule_results=(_schedule_result(moved, later),)),
            repository=repository,
            observed_at=NOW + timedelta(minutes=10),
        )

        stored = await repository.get_lock_in_opportunity(
            LockInOpportunityKey("league-1", 1, 1, "p1", "g1")
        )
        work = await repository.list_scheduled_work(kind=DueWorkKind.POSTGAME)
        assert stored is not None
        assert stored.scheduled_start == moved.start_time
        assert stored.action_deadline == later.start_time - inputs.move_lead_time
        assert stored.status is LockInOpportunityStatus.SCHEDULED
        g1 = next(item for item in work if item.game_id == "g1")
        assert g1.due_at == moved.start_time + timedelta(hours=2)

    asyncio.run(exercise())
