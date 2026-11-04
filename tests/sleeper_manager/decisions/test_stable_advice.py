"""Advice must change only when evidence does, never because time passed between replans."""

import json
from dataclasses import replace
from datetime import timedelta

from sleeper_manager.decisions.team_recommendation import build_team_recommendation
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.domain.planning import ObservedStarter, StarterSlot, TeamWeekState
from sleeper_manager.workflows.plan_rendering import serialize_weekly_plan_trace
from tests.sleeper_manager.decisions.test_joint_completed_scores import _completed
from tests.sleeper_manager.decisions.weekly_plan_support import NOW, _opportunity, _state

LIVE = WeeklyPlanPolicyConfig(
    scenario_count=128, postgame_scenario_count=32, minimum_lock_in_confidence=0.7
)


def _state_near_cutoff(pass_win_rate: float = 0.7) -> TeamWeekState:
    """Passing on the "close" final wins whenever the later swing game scores high."""
    swing = _opportunity(
        "swing",
        "swing-game",
        NOW + timedelta(hours=20),
        ("C",),
        ((200, pass_win_rate), (0, 1 - pass_win_rate)),
        eligible_slot_indices=(1,),
    )
    return replace(
        _state(
            (_completed("sure", 80, ("PG",), (0,)), _completed("close", 50, ("C",), (1,)), swing),
            starter_slots=(StarterSlot(0, "PG"), StarterSlot(1, "C")),
            observed=(
                ObservedStarter(0, "sure", ("PG",)),
                ObservedStarter(1, "close", ("C",)),
            ),
        ),
        week_ends_at=NOW + timedelta(days=6),
    )


def test_replanning_identical_inputs_later_cannot_change_advice() -> None:
    state = _state_near_cutoff()
    plans = [
        build_team_recommendation(
            replace(state, decision_time=state.decision_time + timedelta(minutes=5 * step)),
            policy=LIVE,
        )
        for step in range(12)
    ]
    assert len({plan.material_hash for plan in plans}) == 1
    assert len({tuple(row.confidence for row in plan.score_actions) for plan in plans}) == 1


def test_plan_records_both_confidences_for_a_final_it_leaves_undecided() -> None:
    plan = build_team_recommendation(_state_near_cutoff(pass_win_rate=0.55), policy=LIVE)

    assert [row.player_id for row in plan.undecided_scores] == ["close"]
    undecided = plan.undecided_scores[0]
    assert 0.4 < undecided.pass_confidence < 0.7
    assert 0.3 < undecided.lock_confidence < 0.6
    assert undecided.pass_confidence + undecided.lock_confidence <= 1
    trace = json.loads(serialize_weekly_plan_trace(plan))
    assert trace["undecided_scores"][0]["player_id"] == "close"
