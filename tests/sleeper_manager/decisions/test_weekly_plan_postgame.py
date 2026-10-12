"""Verify that weekly planning retains the later Lock/Pass option."""

from datetime import timedelta
from unittest.mock import patch

import pytest

from sleeper_manager.decisions.simulation import Scenario
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig, score_weekly_options
from sleeper_manager.domain.planning import StarterSlot

from .weekly_plan_support import NOW, _opportunity, _state


def test_planner_starts_to_observe_an_upside_score() -> None:
    state = _state(
        (
            _opportunity("p1", "now", NOW + timedelta(hours=1), ("PG",), ((0, 1), (20, 1))),
            _opportunity("p1", "later", NOW + timedelta(days=1), ("PG",), ((10, 1),)),
        ),
        starter_slots=(StarterSlot(0, "G"),),
    )
    outer = tuple(
        Scenario((("p1:now:segment-1", value), ("p1:later:segment-1", 10))) for value in (0, 20)
    )
    inner = (Scenario((("p1:later:segment-1", 10),)),)
    with patch(
        "sleeper_manager.decisions._weekly_plan_scoring.generate_projection_scenarios",
        side_effect=(outer, inner),
    ):
        decision = score_weekly_options(state, config=WeeklyPlanPolicyConfig(scenario_count=2))
    assert decision.selected.assignments[0].player_id == "p1"
    assert decision.selected.expected_terminal_value == 15
    assert decision.baseline_terminal_value == 10
    assert decision.perfect_information_bound == 15


@pytest.mark.parametrize("count", (0, -1))
def test_postgame_sample_count_must_be_positive(count: int) -> None:
    with pytest.raises(ValueError, match="Postgame scenario count must be positive"):
        WeeklyPlanPolicyConfig(postgame_scenario_count=count)


@pytest.mark.parametrize("confidence", (-0.1, 1.1, float("nan")))
def test_postgame_confidence_must_be_a_probability(confidence: float) -> None:
    with pytest.raises(ValueError, match="confidence must be between zero and one"):
        WeeklyPlanPolicyConfig(minimum_lock_in_confidence=confidence)
