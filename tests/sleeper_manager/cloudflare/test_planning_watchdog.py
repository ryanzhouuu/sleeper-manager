"""Watchdog delivery is based on original schedule age, independent of retry leases."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

import pytest

from sleeper_manager.cloudflare.planning_watchdog import notify_delayed_daily_planning
from sleeper_manager.notifications.dispatcher import NotificationDispatcher
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import (
    DueWorkKind,
    RuntimePolicyRecord,
    ScheduledWorkRecord,
    ScheduledWorkStatus,
)
from sleeper_manager.workflows.notification_loop import (
    DAILY_PLAN_DELAYED_KIND,
    NotificationLoop,
)
from tests.sleeper_manager.cloudflare.test_scheduled_dispatcher import (
    POLICY,
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
        assert sender.messages[0].actions == ()
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
        replace(WORK, status=ScheduledWorkStatus.COMPLETED),
        replace(WORK, status=ScheduledWorkStatus.CANCELED),
        replace(WORK, deadline=NOW),
    ],
)
def test_unactionable_work_stays_quiet(tmp_path, work: ScheduledWorkRecord) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, sender, notifications = await _setup(tmp_path, work=work)
        await _notify(repository, notifications)
        assert sender.messages == []

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "work",
    [
        replace(WORK, attempt_count=1),
        replace(WORK, lease_expires_at=NOW + timedelta(minutes=5)),
        replace(WORK, status=ScheduledWorkStatus.PENDING, attempt_count=0),
        replace(WORK, status=ScheduledWorkStatus.RETRY, due_at=NOW + timedelta(minutes=5)),
    ],
)
def test_overdue_work_alerts_even_with_active_lease_or_future_retry(tmp_path, work) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, sender, notifications = await _setup(tmp_path, work=work)
        await _notify(repository, notifications)
        assert len(sender.messages) == 1
        assert await repository.list_scheduled_work() == (work,)

    asyncio.run(exercise())


def test_retry_reclaim_between_cron_checks_cannot_hide_delay(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, sender, notifications = await _setup(
            tmp_path, work=replace(WORK, lease_expires_at=NOW + timedelta(seconds=36))
        )
        await _notify(repository, notifications)
        claimed = await repository.claim_due_work(
            NOW + timedelta(seconds=38), correlation_id="next"
        )
        assert claimed[0].attempt_count == 3
        await notify_delayed_daily_planning(
            repository,
            notifications,
            league_id="league-1",
            scheduled_at=NOW + timedelta(minutes=5),
            open_sleeper_url="https://sleeper.com",
        )
        assert len(sender.messages) == 1

    asyncio.run(exercise())


def test_missing_daily_work_alerts_without_claiming_unrelated_jobs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        unrelated = replace(WORK, local_day="2026-01-06", dedupe_key="daily:2026-01-06")
        repository, sender, notifications = await _setup(tmp_path, work=unrelated)
        await _notify(repository, notifications)
        assert len(sender.messages) == 1
        assert await repository.list_scheduled_work() == (unrelated,)

    asyncio.run(exercise())


def test_delay_threshold_is_fifteen_minutes_after_original_daily_schedule(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, sender, notifications = await _setup(tmp_path)
        for at in (
            SEVEN_AM_CENTRAL - timedelta(minutes=1),
            SEVEN_AM_CENTRAL + timedelta(minutes=15, microseconds=-1),
        ):
            await notify_delayed_daily_planning(
                repository,
                notifications,
                league_id="league-1",
                scheduled_at=at,
                open_sleeper_url="https://sleeper.com",
            )
        assert sender.messages == []
        await notify_delayed_daily_planning(
            repository,
            notifications,
            league_id="league-1",
            scheduled_at=SEVEN_AM_CENTRAL + timedelta(minutes=15),
            open_sleeper_url="https://sleeper.com",
        )
        assert len(sender.messages) == 1

    asyncio.run(exercise())


def test_watchdog_uses_custom_manager_time_on_dst_transition_day(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        due = datetime(2026, 11, 1, 14, tzinfo=UTC)  # 8 AM after Chicago's fall-back.
        work = replace(WORK, local_day="2026-11-01", dedupe_key="daily:2026-11-01", due_at=due)
        repository, sender, notifications = await _setup(tmp_path, work=work)
        policy = replace(POLICY, daily_plan_time=time(8))
        await repository.save_runtime_policy(
            RuntimePolicyRecord(policy.version, policy.to_json(), due)
        )
        for delta, expected in ((timedelta(minutes=14, seconds=59), 0), (timedelta(minutes=15), 1)):
            await notify_delayed_daily_planning(
                repository,
                notifications,
                league_id="league-1",
                scheduled_at=due + delta,
                open_sleeper_url="https://sleeper.com",
            )
            assert len(sender.messages) == expected

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
