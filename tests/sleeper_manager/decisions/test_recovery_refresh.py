"""Recovery decisions follow observed progress and current deadlines rather than old advice."""

from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.decisions.team_recommendation import build_team_recommendation
from sleeper_manager.domain.planning import PlanningGameStatus, PlanningReasonCode, PlanStatus
from tests.sleeper_manager.decisions.test_recoverable_attempts import POLICY
from tests.sleeper_manager.decisions.test_team_recommendation import _after_trial
from tests.sleeper_manager.decisions.weekly_plan_support import NOW


def _pending_trial(status):
    state = _after_trial(5)
    return replace(
        state,
        opportunities=(
            replace(
                state.opportunities[0],
                status=status,
                completed_fantasy_score=None,
                finalized_at=None,
            ),
            state.opportunities[1],
        ),
    )


@pytest.mark.parametrize("status", (PlanningGameStatus.ACTIVE, PlanningGameStatus.FINAL))
def test_unresolved_trial_is_retained_before_the_recovery_window(status) -> None:
    state = _pending_trial(status)
    plan = build_team_recommendation(state, policy=POLICY)
    assert plan.desired_assignments[0].player_id == "early"
    assert plan.moves == plan.score_actions == plan.forfeited_opportunities == ()
    assert plan.recovery_obligations
    assert plan.expected_terminal_score is None


def test_missing_final_score_can_be_discarded_at_its_named_recovery() -> None:
    state = _pending_trial(PlanningGameStatus.FINAL)
    state = replace(
        state, decision_time=state.opportunities[1].scheduled_start - timedelta(minutes=15)
    )
    plan = build_team_recommendation(state, policy=POLICY)
    assert plan.desired_assignments[0].player_id == "later"
    assert plan.forfeited_opportunities == (("early", "tonight"),)
    assert plan.score_actions == ()


def test_later_tipoff_refreshes_the_pending_recovery_deadline() -> None:
    state = _pending_trial(PlanningGameStatus.ACTIVE)
    later = replace(state.opportunities[1], scheduled_start=NOW + timedelta(days=2))
    state = replace(
        state,
        decision_time=NOW + timedelta(hours=25),
        opportunities=(state.opportunities[0], later),
    )
    plan = build_team_recommendation(state, policy=POLICY)
    assert plan.status is not PlanStatus.BLOCKED
    assert plan.forfeited_opportunities == ()
    assert plan.recovery_obligations[0].recovery_at == later.scheduled_start
    assert plan.recovery_obligations[0].deadline == later.scheduled_start - timedelta(minutes=10)


def test_postponement_keeps_the_obligation_pending_without_unsafe_actions() -> None:
    state = _pending_trial(PlanningGameStatus.ACTIVE)
    state = replace(
        state,
        opportunities=(
            state.opportunities[0],
            replace(state.opportunities[1], status=PlanningGameStatus.POSTPONED),
        ),
    )
    plan = build_team_recommendation(state, policy=POLICY)
    assert plan.status is PlanStatus.BLOCKED
    assert PlanningReasonCode.AMBIGUOUS_GAME_TIME in plan.blocking_reasons
    assert plan.recovery_obligations == state.recovery_obligations
    assert plan.desired_assignments == plan.observed_assignments
    assert plan.moves == plan.score_actions == ()


def test_verified_missed_window_is_reported_without_replaying_a_stale_return() -> None:
    state = _pending_trial(PlanningGameStatus.ACTIVE)
    state = replace(
        state,
        decision_time=NOW + timedelta(hours=25),
        opportunities=(
            state.opportunities[0],
            replace(state.opportunities[1], status=PlanningGameStatus.ACTIVE),
        ),
    )
    plan = build_team_recommendation(state, policy=POLICY)
    assert plan.status is not PlanStatus.BLOCKED
    assert plan.recovery_obligations == ()
    assert plan.recovery_failures == (state.recovery_obligations[0].recovery_id,)
    assert PlanningReasonCode.RECOVERY_WINDOW_CLOSED in plan.warnings
    assert plan.moves == plan.forfeited_opportunities == ()
