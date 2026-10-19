"""Watchdog delivery uses durable deduplication and ignores active, expired, or unrelated work."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.cloudflare.planning_watchdog import notify_delayed_daily_planning
from sleeper_manager.notifications.dispatcher import NotificationDispatcher
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import DueWorkKind, ScheduledWorkRecord, ScheduledWorkStatus
from sleeper_manager.workflows.notification_loop import (
    DAILY_PLAN_DELAYED_KIND,
    NotificationLoop,
)
from tests.sleeper_manager.cloudflare.test_scheduled_dispatcher import (
    SEVEN_AM_CENTRAL,
    _save_policy,
)
from tests.sleeper_manager.workflows.test_notification_loop import RecordingSender

NOW = SEVEN_AM_CENTRAL + timedelta(minutes=30)
WORK = ScheduledWorkRecord(
    "daily-work",
    "daily:2026-01-07",
    DueWorkKind.DAILY,
    SEVEN_AM_CENTRAL,
    ScheduledWorkStatus.RUNNING,
    SEVEN_AM_CENTRAL,
    NOW - timedelta(minutes=15),
    local_day="2026-01-07",
    lease_expires_at=NOW,
    attempt_count=2,
)


async def _setup(tmp_path, *, work=WORK, fail=False):  # type: ignore[no-untyped-def]
    repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
    await repository.initialize()
    await _save_policy(repository, at=NOW)
    await repository.upsert_scheduled_work(work)
    sender = RecordingSender(fail=fail)
    notifications = NotificationLoop(
        repository,
        NotificationDispatcher(sender),
        acknowledgement_base_url="https://example.test/ack",
        clock=lambda: NOW,
    )
    return repository, sender, notifications


async def _notify(repository, notifications):  # type: ignore[no-untyped-def]
    await notify_delayed_daily_planning(
        repository,
        notifications,
        league_id="league-1",
        scheduled_at=NOW,
        open_sleeper_url="https://sleeper.com",
    )


def test_repeated_stale_daily_work_sends_one_durable_alert_without_claiming_work(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, sender, notifications = await _setup(tmp_path)
        await _notify(repository, notifications)
        await _notify(repository, notifications)
        assert len(sender.messages) == 1
        assert sender.messages[0].title == "Daily plan delayed"
        assert [action.label for action in sender.messages[0].actions] == ["Open Sleeper"]
        assert await repository.list_scheduled_work() == (WORK,)
        stored = await repository.list_pending_recommendations(
            "league-1", 0, decision_type=DAILY_PLAN_DELAYED_KIND
        )
        assert len(stored) == 1
        assert await repository.has_successful_delivery(stored[0].recommendation_id)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "work",
    [
        replace(WORK, attempt_count=1),
        replace(WORK, lease_expires_at=NOW + timedelta(minutes=5)),
        replace(WORK, status=ScheduledWorkStatus.COMPLETED),
        replace(WORK, local_day="2026-01-06"),
        replace(WORK, kind=DueWorkKind.PRE_TIPOFF),
        replace(WORK, due_at=NOW + timedelta(minutes=5)),
        replace(WORK, deadline=NOW),
    ],
)
def test_unactionable_work_stays_quiet(tmp_path, work: ScheduledWorkRecord) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, sender, notifications = await _setup(tmp_path, work=work)
        await _notify(repository, notifications)
        assert sender.messages == []

    asyncio.run(exercise())


def test_failed_alert_delivery_remains_retryable_and_then_deduplicates(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, sender, notifications = await _setup(
            tmp_path, work=replace(WORK, status=ScheduledWorkStatus.RETRY), fail=True
        )
        await _notify(repository, notifications)
        sender.fail = False
        await _notify(repository, notifications)
        await _notify(repository, notifications)
        assert len(sender.messages) == 2

    asyncio.run(exercise())
