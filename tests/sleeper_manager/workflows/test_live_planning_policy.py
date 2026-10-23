"""Live plans favor latency while preserving explicit research settings and actual locks."""

from dataclasses import replace

from sleeper_manager.decisions.team_recommendation import build_team_recommendation
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.domain.lock_in import LockInDecisionKind
from sleeper_manager.domain.planning import FixedSlot
from sleeper_manager.workflows.planning_policy import LIVE_PLAN_POLICY
from tests.sleeper_manager.decisions.test_team_recommendation import _after_trial


def test_live_budget_is_smaller_without_changing_research_defaults() -> None:
    assert LIVE_PLAN_POLICY.scenario_count == 128
    assert LIVE_PLAN_POLICY.postgame_scenario_count == 32
    assert WeeklyPlanPolicyConfig().scenario_count == 2000
    assert WeeklyPlanPolicyConfig().postgame_scenario_count == 128


def test_actual_lock_overrides_an_advisor_pass_on_the_next_plan() -> None:
    state = _after_trial(5)
    first = build_team_recommendation(state, policy=LIVE_PLAN_POLICY)
    action = first.score_actions[0]
    assert action.kind is LockInDecisionKind.PASS
    actual = FixedSlot(
        0,
        "UTIL",
        action.player_id,
        action.game_id,
        action.score,
        state.decision_time,
        "owner-confirmed",
        "lock_in_opportunity_v1",
    )
    current = replace(state, fixed_slots=(actual,), recovery_obligations=())
    second = build_team_recommendation(current, policy=LIVE_PLAN_POLICY)
    assert second.fixed_slots == (actual,)
    assert second.desired_assignments[0].player_id == action.player_id
    assert second.expected_terminal_score == 5
    assert second.score_actions == ()


def test_live_sampling_repeats_the_same_plan_for_identical_state() -> None:
    state = _after_trial(80)
    first = build_team_recommendation(state, policy=LIVE_PLAN_POLICY)
    second = build_team_recommendation(state, policy=LIVE_PLAN_POLICY)
    assert first.material_hash == second.material_hash
    assert first.score_actions == second.score_actions
