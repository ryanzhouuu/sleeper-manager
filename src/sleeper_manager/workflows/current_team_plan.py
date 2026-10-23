"""Refresh actual decisions around computation and discard results after concurrent edits."""

from datetime import datetime

from sleeper_manager.decisions.team_recommendation import build_team_recommendation
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.domain.planning import TeamWeekState, WeeklyPlan
from sleeper_manager.persistence.base import AsyncStateRepository
from sleeper_manager.workflows.notification_loop import Clock
from sleeper_manager.workflows.planning_inputs import LivePlanningInputs
from sleeper_manager.workflows.recommendation_state import refreshed_team_state


async def current_decision_version(
    repository: AsyncStateRepository, league_id: str, week: int
) -> str | None:
    read = getattr(repository, "decision_state_version", None)
    version = await read(league_id, week) if read else None
    if version is not None and not isinstance(version, str):
        raise ValueError("Invalid manager decision version")
    return version


async def build_current_team_plan(
    inputs: LivePlanningInputs,
    repository: AsyncStateRepository,
    decision_time: datetime,
    policy: WeeklyPlanPolicyConfig,
    clock: Clock,
    *,
    prepared_plan: WeeklyPlan | None = None,
    prepared_state: TeamWeekState | None = None,
) -> tuple[WeeklyPlan, TeamWeekState, str | None]:
    """Discard computation if even a Wait correction was saved while it was running."""
    league_id, week = inputs.league_profile.league_id, inputs.week_window.week
    version = await current_decision_version(repository, league_id, week)
    state = await refreshed_team_state(inputs, repository, decision_time)
    plan = (
        prepared_plan
        if prepared_plan is not None and prepared_state == state
        else (build_team_recommendation(state, lead_time=inputs.move_lead_time, policy=policy))
    )
    refreshed = await refreshed_team_state(inputs, repository, max(decision_time, clock()))
    if (
        refreshed.fixed_slots != state.fixed_slots
        or refreshed.passed_opportunities != state.passed_opportunities
    ):
        state = refreshed
        plan = build_team_recommendation(state, lead_time=inputs.move_lead_time, policy=policy)
    if await current_decision_version(repository, league_id, week) != version:
        raise RuntimeError("Manager decisions changed during planning; retry with current state")
    return plan, state, version
