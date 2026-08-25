"""Pre-tipoff injury and lineup validation workflow."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, tzinfo

from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.persistence.base import AsyncStateRepository
from sleeper_manager.workflows.daily_plan import (
    WeeklyLineupWorkflowResult,
    _run_weekly_lineup_workflow,
)
from sleeper_manager.workflows.notification_loop import Clock, NotificationLoop
from sleeper_manager.workflows.planning_inputs import LivePlanningInputs


async def run_pre_tipoff_check(
    inputs: LivePlanningInputs,
    *,
    decision_time: datetime,
    repository: AsyncStateRepository,
    notifications: NotificationLoop,
    open_sleeper_url: str,
    player_names: Mapping[str, str] | None = None,
    local_timezone: tzinfo | None = None,
    policy: WeeklyPlanPolicyConfig | None = None,
    clock: Clock | None = None,
) -> WeeklyLineupWorkflowResult:
    return await _run_weekly_lineup_workflow(
        inputs,
        decision_time=decision_time,
        repository=repository,
        notifications=notifications,
        open_sleeper_url=open_sleeper_url,
        player_names=player_names,
        local_timezone=local_timezone,
        policy=policy,
        clock=clock,
        trigger="pre_tipoff",
    )


__all__ = ("run_pre_tipoff_check",)
