"""Owner answers settle unverified games, and alerts name only the open questions."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.domain.lock_in import LockInOpportunityStatus as Status
from sleeper_manager.domain.planning import ObservedStarter, PassedOpportunity, PlanningGameStatus
from sleeper_manager.persistence.lock_in_opportunities import (
    LockInOpportunityKey,
    LockInOpportunityRecord,
)
from sleeper_manager.workflows.availability_advice import state_availability_notes
from sleeper_manager.workflows.availability_rendering import state_availability_summary
from sleeper_manager.workflows.manager_decisions import ManagerAction, reported_decision
from sleeper_manager.workflows.planning_history import player_game_evidence, reconcile_player_games
from tests.sleeper_manager.decisions.weekly_plan_support import NOW as PLAN_NOW
from tests.sleeper_manager.decisions.weekly_plan_support import _opportunity, _state

NOW = datetime(2026, 11, 5, 6, tzinfo=UTC)


def _row(**changes: object) -> LockInOpportunityRecord:
    row = LockInOpportunityRecord(
        key=LockInOpportunityKey("league", 3, 6, "acuff", "g1"),
        provider_player_id="espn-acuff",
        scheduled_start=NOW - timedelta(hours=3),
        action_deadline=NOW + timedelta(days=2),
        fantasy_week_end=NOW + timedelta(days=5),
        status=Status.FINALIZING,
        next_check_at=NOW,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(hours=1),
        slot_index=2,
        slot_position="G",
        eligible_positions=("PG",),
        rostered_at_tipoff=True,
        stable_score=30.0,
        tipoff_unverified=True,
        trace_json=json.dumps({"eligibility_forfeited": False}),
    )
    return replace(row, **changes)  # type: ignore[arg-type]


def test_not_lockable_settles_the_game_as_ineligible() -> None:
    result = reported_decision(_row(), ManagerAction.NOT_LOCKABLE, NOW, actual_slot=(2, "G"))
    assert result.status is Status.INELIGIBLE
    assert result.rostered_at_tipoff is False
    assert result.tipoff_unverified is False
    assert json.loads(result.trace_json)["eligibility_forfeited"] is True


def test_not_lockable_needs_an_unverified_game() -> None:
    with pytest.raises(ValueError, match="unverified"):
        reported_decision(
            _row(tipoff_unverified=False), ManagerAction.NOT_LOCKABLE, NOW, actual_slot=None
        )


def test_locked_clears_the_flag_and_passed_keeps_it_for_corrections() -> None:
    locked = reported_decision(_row(), ManagerAction.LOCKED, NOW, actual_slot=(2, "G"))
    passed = reported_decision(_row(), ManagerAction.PASSED, NOW, actual_slot=(2, "G"))
    assert locked.tipoff_unverified is False
    assert passed.tipoff_unverified is True


def _ineligible(**changes: object) -> LockInOpportunityRecord:
    return _row(
        status=Status.INELIGIBLE,
        rostered_at_tipoff=False,
        tipoff_unverified=False,
        stable_score=None,
        **changes,
    )


def test_override_reopens_an_ineligible_game_for_score_polling() -> None:
    result = reported_decision(_ineligible(), ManagerAction.LOCKABLE, NOW, actual_slot=(2, "G"))
    assert result.status is Status.ACTIVE
    assert result.rostered_at_tipoff is True
    assert result.tipoff_unverified is True
    assert result.next_check_at == NOW
    assert json.loads(result.trace_json)["eligibility_forfeited"] is False


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"action_deadline": NOW}, "deadline"),
        ({"scheduled_start": NOW + timedelta(hours=1)}, "not started"),
    ),
)
def test_override_is_refused_outside_its_window(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        reported_decision(_ineligible(**changes), ManagerAction.LOCKABLE, NOW, actual_slot=None)


def test_override_needs_an_ineligible_game() -> None:
    with pytest.raises(ValueError, match="ineligible"):
        reported_decision(_row(), ManagerAction.LOCKABLE, NOW, actual_slot=None)


def _flagged_state(**changes: object):  # type: ignore[no-untyped-def]
    game = replace(
        _opportunity(
            "acuff",
            "g1",
            PLAN_NOW - timedelta(hours=3),
            ("PG",),
            ((10, 1),),
            status=PlanningGameStatus.FINAL,
            completed_score=30,
            finalized_at=PLAN_NOW - timedelta(minutes=5),
        ),
        tipoff_unverified=True,
    )
    return replace(
        _state((game,), observed=(ObservedStarter(0, "acuff", ("PG",)),)),
        **changes,
    )


def test_alert_and_trace_tell_the_owner_to_check_sleeper() -> None:
    names = {"acuff": "Darius Acuff Jr."}
    state = _flagged_state()
    summary = state_availability_summary(state, player_names=names)
    notes = state_availability_notes(state, player_names=names)
    assert any("Darius Acuff Jr. may have been moved in" in line for line in summary)
    assert any("Not lockable" in line for line in notes)


def test_a_recorded_pass_stops_the_check_line() -> None:
    state = _flagged_state(
        passed_opportunities=(
            PassedOpportunity("acuff", "g1", PLAN_NOW, "decision", "lock_in_opportunity_v1"),
        )
    )
    assert state_availability_summary(state) == ()


def test_flag_reaches_the_planner_as_a_lockable_game() -> None:
    row = _row(
        key=LockInOpportunityKey("league", 1, 1, "acuff", "g1"),
        scheduled_start=PLAN_NOW - timedelta(hours=3),
        action_deadline=PLAN_NOW + timedelta(days=1),
        updated_at=PLAN_NOW - timedelta(minutes=1),
        stabilized_at=PLAN_NOW - timedelta(minutes=5),
        stable_fingerprint="final",
        current_observation_fingerprint="final",
    )
    game = _opportunity("acuff", "g1", row.scheduled_start, ("PG",), ((10, 1),), rostered=None)
    reconciled = reconcile_player_games(
        [game],
        (player_game_evidence(row),),
        (ObservedStarter(0, "acuff", ("PG",)),),
        PLAN_NOW,
    )[0]
    assert reconciled.tipoff_unverified is True
    assert reconciled.rostered_at_tipoff is True
    assert reconciled.completed_fantasy_score == 30
    assert reconciled.eligible_slot_indices
