"""Use a smaller deterministic Monte Carlo budget for interactive and scheduled live advice."""

from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig

LIVE_PLAN_POLICY = WeeklyPlanPolicyConfig(scenario_count=128, postgame_scenario_count=32)
