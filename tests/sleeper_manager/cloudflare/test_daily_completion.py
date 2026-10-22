"""Scheduled daily work confirms no-change plans once, including coalesced tipoff wakes."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.cloudflare.dispatcher import dispatch_due_work
from sleeper_manager.persistence.base import DueWorkKind, ScheduledWorkRecord, ScheduledWorkStatus
from tests.sleeper_manager.cloudflare.test_scheduled_dispatcher import (
    NOW,
    PLAN_POLICY,
    _assembly,
    _collector,
    _save_policy,
)
from tests.sleeper_manager.workflows.test_daily_plan import _profile, _workflow


def _settled_assembly():  # type: ignore[no-untyped-def]
    assembly = _assembly()
    inputs = replace(assembly.evidence.inputs, league_profile=_profile(starter_ids=("p1", "p2")))
    return replace(assembly, evidence=replace(assembly.evidence, inputs=inputs))


@pytest.mark.parametrize("coalesced", [False, True])
def test_daily_no_change_plan_sends_one_confirmation(tmp_path, coalesced) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, sender, notifications = _workflow(tmp_path, clock=lambda: NOW)
        await repository.initialize()
        await _save_policy(repository)
        if coalesced:
            await repository.upsert_scheduled_work(
                ScheduledWorkRecord(
                    work_id="pre-tipoff",
                    dedupe_key="pre_tipoff:current",
                    kind=DueWorkKind.PRE_TIPOFF,
                    due_at=NOW,
                    status=ScheduledWorkStatus.PENDING,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        collect, calls = _collector(_settled_assembly())
        for index in range(2):
            result = await dispatch_due_work(
                repository,
                notifications=notifications,
                collect=collect,
                scheduled_at=NOW,
                correlation_id=f"run-{index}",
                open_sleeper_url="https://sleeper.com",
                plan_policy=PLAN_POLICY,
            )
            if index == 0:
                assert result.status.value == "success"
                assert all(attempt.plan_status == "no_action" for attempt in result.attempts)
        assert calls == [NOW]
        assert len(sender.messages) == 1
        assert sender.messages[0].title == "Lineup is set"
        assert sender.messages[0].actions == ()

    asyncio.run(exercise())


def test_failed_daily_confirmation_retries_without_replanning(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, sender, notifications = _workflow(tmp_path, clock=lambda: NOW)
        sender.fail = True
        await repository.initialize()
        await _save_policy(repository)
        collect, calls = _collector(_settled_assembly())

        async def run(at, correlation):  # type: ignore[no-untyped-def]
            return await dispatch_due_work(
                repository,
                notifications=notifications,
                collect=collect,
                scheduled_at=at,
                correlation_id=correlation,
                open_sleeper_url="https://sleeper.com",
                plan_policy=PLAN_POLICY,
            )

        first = await run(NOW, "first")
        assert first.status.value == "delivery_failed"
        sender.fail = False
        second = await run(NOW + timedelta(minutes=5), "retry")
        assert second.status.value == "success"
        await run(NOW + timedelta(minutes=5), "duplicate")
        assert calls == [NOW]
        assert len(sender.messages) == 2
        assert sender.messages[0] == sender.messages[1]

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", [DueWorkKind.PRE_TIPOFF, DueWorkKind.DAILY])
def test_later_tipoff_or_recovery_work_stays_quiet(tmp_path, kind) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, sender, notifications = _workflow(tmp_path, clock=lambda: NOW)
        await repository.initialize()
        await _save_policy(repository)
        collect, _ = _collector(_settled_assembly())
        for index in range(2):
            if index == 1:
                await repository.upsert_scheduled_work(
                    ScheduledWorkRecord(
                        work_id="later",
                        dedupe_key="later",
                        kind=kind,
                        due_at=NOW,
                        status=ScheduledWorkStatus.PENDING,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
            result = await dispatch_due_work(
                repository,
                notifications=notifications,
                collect=collect,
                scheduled_at=NOW,
                correlation_id=f"run-{index}",
                open_sleeper_url="https://sleeper.com",
                plan_policy=PLAN_POLICY,
            )
        assert result.status.value == "no_action"
        assert len(sender.messages) == 1

    asyncio.run(exercise())
