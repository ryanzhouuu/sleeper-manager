"""Advice must change only when evidence does, never because time passed between replans."""

from dataclasses import replace
from datetime import timedelta

from sleeper_manager.decisions.team_recommendation import build_team_recommendation
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.domain.planning import ObservedStarter, StarterSlot, TeamWeekState
from tests.sleeper_manager.decisions.test_joint_completed_scores import _completed
from tests.sleeper_manager.decisions.weekly_plan_support import NOW, _opportunity, _state

LIVE = WeeklyPlanPolicyConfig(
    scenario_count=128, postgame_scenario_count=32, minimum_lock_in_confidence=0.7
)


def _state_at_confidence_cutoff() -> TeamWeekState:
    """Passing beats this final in about 70% of draws, which sits exactly on the cutoff."""
    swing = _opportunity(
        "swing",
        "swing-game",
        NOW + timedelta(hours=20),
        ("C",),
        ((200, 0.7), (0, 0.3)),
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
    state = _state_at_confidence_cutoff()
    plans = [
        build_team_recommendation(
            replace(state, decision_time=state.decision_time + timedelta(minutes=5 * step)),
            policy=LIVE,
        )
        for step in range(12)
    ]
    assert len({plan.material_hash for plan in plans}) == 1
    assert len({tuple(row.confidence for row in plan.score_actions) for plan in plans}) == 1
