"""Eligibility history survives refreshes independently of whether advice was followed."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.domain.planning import ObservedStarter, PlanningGameStatus
from sleeper_manager.workflows.lock_in_planning import _merge_planning_evidence
from sleeper_manager.workflows.planning_history import player_game_evidence, reconcile_player_games
from tests.sleeper_manager.decisions.weekly_plan_support import NOW, _opportunity
from tests.sleeper_manager.persistence.test_lock_in_opportunity_persistence import (
    _opportunity as _record,
)
from tests.sleeper_manager.persistence.test_lock_in_opportunity_persistence import _repository


def _observed_score():
    """Use a stable score with a still-open eligibility window."""
    row = _record()
    return replace(
        row,
        key=replace(row.key, player_id="p1", game_id="g1", roster_id=1),
        scheduled_start=NOW - timedelta(hours=3),
        created_at=NOW - timedelta(days=1),
        updated_at=NOW,
        action_deadline=NOW + timedelta(hours=4),
        stabilized_at=NOW,
        stable_score=40,
        stable_fingerprint="stable",
        current_observation_fingerprint="stable",
    )


def test_stable_completed_option_survives_advice_without_confirmation() -> None:
    row = _observed_score()
    game = _opportunity("p1", "g1", row.scheduled_start, ("PG",), ((20, 1),))
    starter = (ObservedStarter(1, "p1", ("PG",)),)
    result = reconcile_player_games([game], (player_game_evidence(row),), starter, NOW)[0]
    assert result.completed_fantasy_score == 40
    assert result.status is PlanningGameStatus.FINAL
    assert result.eligible_slot_indices == (0, 1)
    assert (
        reconcile_player_games([game], (player_game_evidence(row),), (), NOW)[
            0
        ].eligible_slot_indices
        == ()
    )


@pytest.mark.parametrize(
    "status",
    (
        LockInOpportunityStatus.INELIGIBLE,
        LockInOpportunityStatus.EXPIRED,
        LockInOpportunityStatus.ACKNOWLEDGED_PASSED,
    ),
)
def test_unavailable_performances_do_not_revive(status) -> None:
    row = replace(_observed_score(), status=status)
    game = _opportunity("p1", "g1", row.scheduled_start, ("PG",), ((20, 1),))
    result = reconcile_player_games(
        [game], (player_game_evidence(row),), (ObservedStarter(0, "p1", ("PG",)),), NOW
    )[0]
    assert result.eligible_slot_indices == ()


def test_pending_correction_cannot_supply_a_new_completed_option() -> None:
    row = replace(_observed_score(), current_observation_fingerprint="correction")
    game = _opportunity("p1", "g1", row.scheduled_start, ("PG",), ((20, 1),))
    assert (
        reconcile_player_games([game], (player_game_evidence(row),), (), NOW)[
            0
        ].completed_fantasy_score
        is None
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        (LockInOpportunityStatus.ACTIVE, PlanningGameStatus.ACTIVE),
        (LockInOpportunityStatus.FINALIZING, PlanningGameStatus.FINAL),
    ),
)
def test_retained_progress_overrides_a_stale_schedule_for_every_player(status, expected) -> None:
    row = replace(
        _observed_score(),
        status=status,
        stable_score=None,
        stable_fingerprint=None,
        current_observation_fingerprint=None,
        stabilized_at=None,
    )
    games = [
        _opportunity(player, "g1", row.scheduled_start, ("PG",), ((20, 1),))
        for player in ("p1", "p2")
    ]
    result = reconcile_player_games(
        games, (player_game_evidence(row),), (ObservedStarter(0, "p1", ("PG",)),), NOW
    )
    assert all(game.status is expected for game in result)
    assert all(game.completed_fantasy_score is None for game in result)


def test_postponement_does_not_turn_a_pre_tipoff_bench_observation_into_forfeiture() -> None:
    current = replace(
        _observed_score(),
        status=LockInOpportunityStatus.INELIGIBLE,
        updated_at=NOW - timedelta(hours=4),
        stable_score=None,
        stable_fingerprint=None,
        current_observation_fingerprint=None,
        stabilized_at=None,
    )
    candidate = replace(
        current,
        status=LockInOpportunityStatus.RECONCILIATION_REQUIRED,
        updated_at=NOW,
        roster_evidence_at=None,
        trace_json='{"observed_starter":false}',
    )
    result = _merge_planning_evidence(current, candidate)
    assert result.status is LockInOpportunityStatus.RECONCILIATION_REQUIRED


def test_stale_schedule_wakes_do_not_manufacture_a_verified_bench_transition() -> None:
    current = replace(
        _observed_score(),
        status=LockInOpportunityStatus.INELIGIBLE,
        updated_at=NOW - timedelta(hours=4),
        stable_score=None,
        stable_fingerprint=None,
        current_observation_fingerprint=None,
        stabilized_at=None,
    )
    stale = replace(
        current,
        status=LockInOpportunityStatus.SCHEDULED,
        updated_at=NOW,
        trace_json='{"observed_starter":false}',
        roster_evidence_at=None,
    )
    pending = _merge_planning_evidence(current, stale)
    postponed = replace(stale, status=LockInOpportunityStatus.RECONCILIATION_REQUIRED)
    assert _merge_planning_evidence(pending, postponed).status is (
        LockInOpportunityStatus.RECONCILIATION_REQUIRED
    )


def test_shared_final_status_never_erases_a_verified_player_score_timestamp() -> None:
    row = _observed_score()
    games = [
        _opportunity(
            player, "g1", row.scheduled_start, ("PG",), ((20, 1),), status=PlanningGameStatus.FINAL
        )
        for player in ("p1", "p2")
    ]
    result = reconcile_player_games(
        games, (player_game_evidence(row),), (ObservedStarter(0, "p1", ("PG",)),), NOW
    )
    assert result[0].completed_fantasy_score == 40
    assert result[0].finalized_at == row.stabilized_at


def test_observed_active_progress_survives_a_stale_scheduled_refresh() -> None:
    current = replace(_observed_score(), status=LockInOpportunityStatus.ACTIVE)
    candidate = replace(current, status=LockInOpportunityStatus.SCHEDULED)
    assert _merge_planning_evidence(current, candidate).status is LockInOpportunityStatus.ACTIVE


def test_observed_bench_transition_is_irreversible() -> None:
    current = _observed_score()
    candidate = replace(
        current,
        updated_at=NOW + timedelta(minutes=5),
        roster_evidence_at=None,
        trace_json='{"observed_starter":false}',
    )
    forfeited = _merge_planning_evidence(current, candidate)
    assert forfeited.status is LockInOpportunityStatus.INELIGIBLE
    restored = _merge_planning_evidence(
        forfeited, replace(candidate, trace_json='{"observed_starter":true}')
    )
    assert restored.status is LockInOpportunityStatus.INELIGIBLE


@pytest.mark.parametrize("backend", ("sqlite", "d1"))
def test_all_week_evidence_is_scoped_and_persisted(backend: str, tmp_path: Path) -> None:
    async def exercise() -> None:
        repository = await _repository(backend, tmp_path)
        row = _observed_score()
        await repository.upsert_lock_in_opportunity(row)
        other = replace(row, key=replace(row.key, fantasy_week=2))
        await repository.upsert_lock_in_opportunity(other)
        assert await repository.list_week_lock_in_opportunities(row.key.league_id, 1) == (row,)
        assert await repository.load_acknowledged_lock_in_opportunities(row.key.league_id, 1) == ()

    asyncio.run(exercise())


def test_past_game_without_retained_tipoff_evidence_stays_unknown() -> None:
    from sleeper_manager.workflows.planning_inputs import build_live_team_week_state
    from tests.sleeper_manager.workflows.planning_inputs_support import (
        NOW as LIVE_NOW,
    )
    from tests.sleeper_manager.workflows.planning_inputs_support import (
        _game,
        _inputs,
        _schedule_result,
    )

    inputs = _inputs(
        schedule_results=(_schedule_result(_game("past", start=LIVE_NOW - timedelta(hours=3))),)
    )
    state = build_live_team_week_state(inputs, decision_time=LIVE_NOW)
    assert all(game.rostered_at_tipoff is None for game in state.opportunities)


def test_legacy_automatic_final_is_not_an_accepted_lock_before_week_end() -> None:
    from sleeper_manager.workflows.lock_in_evidence import evidence_from_lock_in_opportunity

    row = replace(
        _observed_score(),
        status=LockInOpportunityStatus.AUTOMATIC_FINAL,
        fantasy_week_end=NOW + timedelta(days=1),
    )
    assert evidence_from_lock_in_opportunity(row) is None
