"""Reuse identical inner and outer draws across hypothetical states at one decision cutoff."""

from dataclasses import dataclass
from datetime import datetime

from sleeper_manager.decisions._weekly_plan_models import WeeklyPlanPolicyConfig
from sleeper_manager.decisions.simulation import (
    Scenario,
    ScenarioInput,
    generate_projection_scenarios,
    stable_scenario_seed,
)
from sleeper_manager.domain.planning import TeamWeekState


@dataclass(frozen=True, slots=True)
class PreparedPlanSamples:
    """Bind common random draws to their original universe, policy, and team decision."""

    decision: tuple[str, int, int, datetime]
    policy: WeeklyPlanPolicyConfig
    inputs: tuple[ScenarioInput, ...]
    outer: tuple[Scenario, ...]
    inner: tuple[Scenario, ...]

    @classmethod
    def prepare(
        cls,
        state: TeamWeekState,
        policy: WeeklyPlanPolicyConfig,
        inputs: tuple[ScenarioInput, ...],
    ) -> "PreparedPlanSamples":
        seed = stable_scenario_seed(
            policy.seed,
            league_id=state.league_id,
            week=state.week,
            roster_id=state.roster_id,
            decision_time=state.decision_time,
        )
        return cls(
            (state.league_id, state.week, state.roster_id, state.decision_time),
            policy,
            inputs,
            generate_projection_scenarios(
                inputs, decision_time=state.decision_time, count=policy.scenario_count, seed=seed
            ),
            generate_projection_scenarios(
                inputs,
                decision_time=state.decision_time,
                count=policy.postgame_scenario_count,
                seed=seed ^ 0x504F5354,
            ),
        )

    def validate(
        self,
        state: TeamWeekState,
        policy: WeeklyPlanPolicyConfig,
        inputs: tuple[ScenarioInput, ...],
    ) -> None:
        if (
            self.decision != (state.league_id, state.week, state.roster_id, state.decision_time)
            or self.policy != policy
            or self.inputs != inputs
        ):
            raise ValueError("Prepared samples belong to a different decision")
