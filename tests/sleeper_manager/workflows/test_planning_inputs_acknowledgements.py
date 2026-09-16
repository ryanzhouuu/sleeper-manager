"""Acknowledged lock/pass conversion into live team-week constraints."""

from datetime import timedelta

from sleeper_manager.domain.planning import (
    AcknowledgedAction,
    AcknowledgedDecisionEvidence,
    PlanningReasonCode,
)
from sleeper_manager.workflows.planning_inputs import (
    LiveProjectionResult,
    build_live_team_week_state,
)
from tests.sleeper_manager.workflows.planning_inputs_support import (
    NOW,
    _inputs,
    _snapshot,
)


def test_acknowledged_lock_becomes_fixed_slot() -> None:
    lock = AcknowledgedDecisionEvidence(
        decision_id="rec-lock-1",
        player_id="p1",
        game_id="g1",
        action=AcknowledgedAction.LOCK,
        decided_at=NOW - timedelta(hours=1),
        provenance="notification-token",
        slot_index=0,
        slot_position="PG",
        accepted_fantasy_score=24,
    )
    inputs = _inputs(
        projections=(LiveProjectionResult("p1", "g1", _snapshot("p1", "g1"), None),),
        acknowledgements=(lock,),
    )
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert len(state.fixed_slots) == 1
    assert state.open_slot_indices == (1,)
    assert state.fixed_slots[0].accepted_fantasy_score == 24


def test_acknowledged_pass_removes_one_opportunity_only() -> None:
    passed = AcknowledgedDecisionEvidence(
        decision_id="rec-pass-1",
        player_id="p1",
        game_id="g1",
        action=AcknowledgedAction.PASS,
        decided_at=NOW - timedelta(hours=1),
        provenance="notification-token",
    )
    inputs = _inputs(acknowledgements=(passed,))
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert len(state.passed_opportunities) == 1
    remaining = {
        (opportunity.sleeper_player_id, opportunity.game_id)
        for opportunity in state.unpassed_opportunities
    }
    assert ("p1", "g1") not in remaining
    assert ("p1", "g2") in remaining


def test_conflicting_acknowledgements_block_without_conversions() -> None:
    first_lock = AcknowledgedDecisionEvidence(
        decision_id="rec-a",
        player_id="p1",
        game_id="g1",
        action=AcknowledgedAction.LOCK,
        decided_at=NOW - timedelta(hours=2),
        provenance="token",
        slot_index=0,
        slot_position="PG",
        accepted_fantasy_score=10,
    )
    second_lock = AcknowledgedDecisionEvidence(
        decision_id="rec-b",
        player_id="p2",
        game_id="g2",
        action=AcknowledgedAction.LOCK,
        decided_at=NOW - timedelta(hours=1),
        provenance="token",
        slot_index=0,
        slot_position="PG",
        accepted_fantasy_score=12,
    )
    unreconciled = AcknowledgedDecisionEvidence(
        decision_id="rec-c",
        player_id="p1",
        game_id="g2",
        action=AcknowledgedAction.PASS,
        decided_at=NOW - timedelta(minutes=30),
        provenance="repository-query",
        reconciled=False,
    )
    inputs = _inputs(acknowledgements=(first_lock, second_lock, unreconciled))
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert len(state.fixed_slots) == 1
    assert state.passed_opportunities == ()
    assert PlanningReasonCode.ACKNOWLEDGEMENT_CONFLICT in state.blocking_reasons


def test_lock_with_unknown_slot_becomes_conflict_not_abort() -> None:
    invalid_lock = AcknowledgedDecisionEvidence(
        decision_id="rec-x",
        player_id="p1",
        game_id="g1",
        action=AcknowledgedAction.LOCK,
        decided_at=NOW - timedelta(hours=1),
        provenance="repository-query",
        slot_index=99,
        slot_position="PG",
        accepted_fantasy_score=10,
    )
    inputs = _inputs(acknowledgements=(invalid_lock,))
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert state.fixed_slots == ()
    assert PlanningReasonCode.ACKNOWLEDGEMENT_CONFLICT in state.blocking_reasons


def test_lock_with_wrong_slot_position_becomes_conflict() -> None:
    misplaced = AcknowledgedDecisionEvidence(
        decision_id="rec-y",
        player_id="p1",
        game_id="g1",
        action=AcknowledgedAction.LOCK,
        decided_at=NOW - timedelta(hours=1),
        provenance="repository-query",
        slot_index=1,
        slot_position="PG",
        accepted_fantasy_score=10,
    )
    inputs = _inputs(acknowledgements=(misplaced,))
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert state.fixed_slots == ()
    assert PlanningReasonCode.ACKNOWLEDGEMENT_CONFLICT in state.blocking_reasons


def test_lock_for_ineligible_player_becomes_conflict() -> None:
    invalid_lock = AcknowledgedDecisionEvidence(
        decision_id="rec-ineligible",
        player_id="p2",
        game_id="g1",
        action=AcknowledgedAction.LOCK,
        decided_at=NOW - timedelta(hours=1),
        provenance="repository-query",
        slot_index=0,
        slot_position="PG",
        accepted_fantasy_score=10,
    )
    inputs = _inputs(acknowledgements=(invalid_lock,))
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert state.fixed_slots == ()
    assert PlanningReasonCode.ACKNOWLEDGEMENT_CONFLICT in state.blocking_reasons


def test_future_dated_acknowledgement_becomes_conflict() -> None:
    future_pass = AcknowledgedDecisionEvidence(
        decision_id="rec-z",
        player_id="p1",
        game_id="g1",
        action=AcknowledgedAction.PASS,
        decided_at=NOW + timedelta(minutes=1),
        provenance="repository-query",
    )
    inputs = _inputs(acknowledgements=(future_pass,))
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert state.passed_opportunities == ()
    assert PlanningReasonCode.ACKNOWLEDGEMENT_CONFLICT in state.blocking_reasons
