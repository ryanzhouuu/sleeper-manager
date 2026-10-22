"""Changed recovery deadlines receive durable checks even after an earlier check completed."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from sleeper_manager.decisions.team_recommendation import build_team_recommendation
from sleeper_manager.persistence.base import DueWorkKind, ScheduledWorkStatus
from sleeper_manager.workflows.recovery_schedule import schedule_recovery_checks
from tests.sleeper_manager.decisions.test_recoverable_attempts import POLICY, _trial
from tests.sleeper_manager.decisions.weekly_plan_support import NOW
from tests.sleeper_manager.persistence.test_lock_in_opportunity_persistence import _repository


@pytest.mark.parametrize("backend", ("sqlite", "d1"))
def test_rescheduled_recovery_is_rearmed_after_its_original_check_completed(
    backend: str, tmp_path: Path
) -> None:
    async def exercise() -> None:
        repository = await _repository(backend, tmp_path)
        plan = build_team_recommendation(_trial(), policy=POLICY)
        await schedule_recovery_checks(repository, plan, NOW)
        check = (await repository.list_scheduled_work(kind=DueWorkKind.DAILY))[0]
        await repository.upsert_scheduled_work(replace(check, status=ScheduledWorkStatus.COMPLETED))
        recovery = plan.recovery_obligations[0]
        later = replace(
            recovery,
            recovery_at=recovery.recovery_at + timedelta(days=1),
            deadline=recovery.deadline + timedelta(days=1),
        )
        updated = replace(plan, recovery_obligations=(later,))
        await schedule_recovery_checks(repository, updated, check.due_at)
        await schedule_recovery_checks(repository, updated, check.due_at)
        checks = await repository.list_scheduled_work(kind=DueWorkKind.DAILY)
        assert len(checks) == 2
        assert {row.status for row in checks} == {
            ScheduledWorkStatus.COMPLETED,
            ScheduledWorkStatus.PENDING,
        }
        assert any(row.due_at == later.deadline - timedelta(minutes=5) for row in checks)

    asyncio.run(exercise())
