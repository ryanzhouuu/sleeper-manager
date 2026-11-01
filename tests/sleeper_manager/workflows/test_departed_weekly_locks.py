"""A departed player's accepted weekly score remains fixed without restoring ownership."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from sleeper_manager.decisions.team_recommendation import build_team_recommendation
from sleeper_manager.domain.lock_in import LockInOpportunityStatus as Status
from sleeper_manager.domain.planning import PlanningReasonCode
from sleeper_manager.workflows.recommendation_state import refreshed_team_state
from tests.sleeper_manager.persistence.test_lock_in_opportunity_persistence import (
    _opportunity,
    _repository,
)
from tests.sleeper_manager.workflows.planning_inputs_support import (
    NOW,
    _game,
    _inputs,
    _profile,
    _schedule_result,
)


def departed_lock():  # type: ignore[no-untyped-def]
    """Keep the accepted game absent from the currently owned players' schedules."""
    original = _opportunity()
    return replace(
        original,
        key=replace(original.key, league_id="league-1", roster_id=1, player_id="p1", game_id="g1"),
        status=Status.ACKNOWLEDGED_LOCKED,
        scheduled_start=NOW - timedelta(days=1),
        action_deadline=NOW - timedelta(hours=1),
        fantasy_week_end=NOW + timedelta(days=2),
        slot_index=0,
        slot_position="PG",
        eligible_positions=("PG",),
        rostered_at_tipoff=True,
        stable_score=54.9,
        stable_fingerprint="accepted",
        current_observation_fingerprint="accepted",
        stabilized_at=NOW - timedelta(hours=20),
        acknowledged_action="locked",
        acknowledged_at=NOW - timedelta(hours=19),
        updated_at=NOW - timedelta(minutes=5),
    )


@pytest.mark.parametrize("backend", ["sqlite", "d1"])
def test_departed_lock_retains_score_and_slot_without_future_opportunities(
    backend: str, tmp_path: Path
) -> None:
    async def exercise() -> None:
        repository = await _repository(backend, tmp_path)
        accepted = departed_lock()
        await repository.upsert_lock_in_opportunity(accepted)
        inputs = _inputs(
            _profile(player_ids=("p2",), starter_ids=(None, "p2")),
            schedule_results=(_schedule_result(_game("g2")),),
        )

        state = await refreshed_team_state(inputs, repository, NOW)

        assert not state.is_blocked
        assert [
            (row.player_id, row.slot_index, row.accepted_fantasy_score) for row in state.fixed_slots
        ] == [("p1", 0, 54.9)]
        assert state.open_slot_indices == (1,)
        assert [(row.player_id, row.slot_index) for row in state.observed_starters] == [
            ("p2", 1),
            ("p1", 0),
        ]
        games = [row for row in state.opportunities if row.sleeper_player_id == "p1"]
        assert len(games) == 1 and games[0].game_id == "g1"
        assert games[0].projection is None
        assert games[0].completed_fantasy_score == 54.9
        assert inputs.league_profile.rosters[0].player_ids == ("p2",)
        assert inputs.league_profile.rosters[0].starter_ids == (None, "p2")
        assert (await repository.get_lock_in_opportunity(accepted.key)) == accepted
        plan = build_team_recommendation(state)
        assert plan.fixed_slots == state.fixed_slots
        assert all(action.player_id != "p1" for action in plan.score_actions)
        assert all(move.outgoing_player_id != "p1" for move in plan.moves)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "overrides",
    [
        {"rostered_at_tipoff": False},
        {"eligible_positions": ("C",)},
        {"slot_position": "SF"},
        {"acknowledged_at": NOW + timedelta(minutes=1)},
        {"updated_at": NOW + timedelta(minutes=1)},
        {"scheduled_start": NOW - timedelta(days=10)},
    ],
)
def test_unverified_departed_lock_still_blocks(tmp_path: Path, overrides: dict) -> None:
    async def exercise() -> None:
        repository = await _repository("sqlite", tmp_path)
        await repository.upsert_lock_in_opportunity(replace(departed_lock(), **overrides))
        inputs = _inputs(_profile(player_ids=("p2",), starter_ids=(None, "p2")))
        state = await refreshed_team_state(inputs, repository, NOW)
        assert PlanningReasonCode.ACKNOWLEDGEMENT_CONFLICT in state.blocking_reasons
        assert not state.fixed_slots

    asyncio.run(exercise())


def test_departed_lock_cannot_overwrite_an_observed_starter(tmp_path: Path) -> None:
    async def exercise() -> None:
        repository = await _repository("sqlite", tmp_path)
        await repository.upsert_lock_in_opportunity(departed_lock())
        inputs = _inputs(_profile(player_ids=("p2",), starter_ids=("p2", None)))
        state = await refreshed_team_state(inputs, repository, NOW)
        assert PlanningReasonCode.ACKNOWLEDGEMENT_CONFLICT in state.blocking_reasons
        assert all(row.player_id != "p1" for row in state.observed_starters)

    asyncio.run(exercise())


def test_duplicate_departed_locks_block_without_invalid_state(tmp_path: Path) -> None:
    async def exercise() -> None:
        repository = await _repository("sqlite", tmp_path)
        first = departed_lock()
        second = replace(first, key=replace(first.key, game_id="other-game"))
        for accepted in (first, second):
            await repository.upsert_lock_in_opportunity(accepted)
        inputs = _inputs(
            _profile(player_ids=("p2",), starter_ids=(None, "p2")),
            schedule_results=(_schedule_result(_game("g2")),),
        )
        state = await refreshed_team_state(inputs, repository, NOW)
        assert PlanningReasonCode.ACKNOWLEDGEMENT_CONFLICT in state.blocking_reasons
        assert len(state.fixed_slots) == 1

    asyncio.run(exercise())
