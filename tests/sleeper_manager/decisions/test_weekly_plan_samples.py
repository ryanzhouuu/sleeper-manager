"""Shared draws preserve scoring and remain scoped to one decision and sampling policy."""

from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.decisions._weekly_plan_inputs import future_opportunities, scenario_input
from sleeper_manager.decisions._weekly_plan_samples import PreparedPlanSamples
from sleeper_manager.decisions.weekly_plan import score_weekly_options
from tests.sleeper_manager.decisions.test_recoverable_attempts import POLICY, _trial


def test_prepared_draws_preserve_complete_weekly_decision() -> None:
    state = _trial()
    inputs = tuple(
        scenario_input(game) for game in future_opportunities(state, state.decision_time)
    )
    prepared = PreparedPlanSamples.prepare(state, POLICY, inputs)

    assert score_weekly_options(
        state, config=POLICY, capture_samples=True, sampling_inputs=inputs, _samples=prepared
    ) == score_weekly_options(state, config=POLICY, capture_samples=True, sampling_inputs=inputs)


@pytest.mark.parametrize("change", ["time", "roster", "policy", "inputs"])
def test_prepared_draws_reject_a_different_decision(change: str) -> None:
    state = _trial()
    inputs = tuple(
        scenario_input(game) for game in future_opportunities(state, state.decision_time)
    )
    prepared = PreparedPlanSamples.prepare(state, POLICY, inputs)
    config = POLICY
    if change == "time":
        state = replace(state, decision_time=state.decision_time + timedelta(seconds=1))
    elif change == "roster":
        state = replace(state, league_id="another-league")
    elif change == "policy":
        config = replace(config, seed=config.seed + 1)
    else:
        inputs = inputs[:-1]

    with pytest.raises(ValueError, match="different decision"):
        score_weekly_options(state, config=config, sampling_inputs=inputs, _samples=prepared)


def test_joint_comparison_prepares_draws_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from sleeper_manager.decisions import _weekly_plan_samples
    from sleeper_manager.decisions.team_recommendation import build_team_recommendation
    from tests.sleeper_manager.decisions.test_team_recommendation import _after_trial

    original = _weekly_plan_samples.generate_projection_scenarios
    counts: list[int] = []

    def record(*args, **kwargs):
        counts.append(kwargs["count"])
        return original(*args, **kwargs)

    monkeypatch.setattr(_weekly_plan_samples, "generate_projection_scenarios", record)
    plan = build_team_recommendation(_after_trial(80), policy=POLICY)

    assert plan.expected_terminal_score == 80
    assert counts == [POLICY.scenario_count, POLICY.postgame_scenario_count]
