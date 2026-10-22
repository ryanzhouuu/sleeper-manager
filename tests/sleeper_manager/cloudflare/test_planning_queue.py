"""Queue transport defers expensive work while durable leases preserve delivery semantics."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from sleeper_manager.cloudflare.planning_queue import MAX_WAKE_AGE, PlanningWake
from sleeper_manager.cloudflare.runtime import run_queued_planning, run_scheduled
from sleeper_manager.notifications.dispatcher import NotificationDispatcher
from sleeper_manager.persistence.base import DueWorkKind, ScheduledWorkStatus
from sleeper_manager.persistence.d1 import D1_SCHEMA, D1StateRepository
from tests.sleeper_manager.cloudflare.test_scheduled_dispatcher import NOW, _assembly, _save_policy
from tests.sleeper_manager.persistence.test_d1 import FakeD1
from tests.sleeper_manager.workflows.test_notification_loop import RecordingSender


class Queue:
    """Retain the exact envelope crossing the queue binding."""

    def __init__(self) -> None:
        self.bodies: list[str] = []

    async def send(self, body: str, *, contentType: str) -> None:
        assert contentType == "text"
        self.bodies.append(body)


def test_wake_round_trips_and_has_bounded_age() -> None:
    wake = PlanningWake(NOW)
    assert PlanningWake.from_body(wake.to_body()) == wake
    assert wake.is_current(NOW)
    assert wake.is_current(NOW + MAX_WAKE_AGE)
    assert not wake.is_current(NOW + MAX_WAKE_AGE + timedelta(microseconds=1))
    assert not wake.is_current(NOW - timedelta(microseconds=1))
    with pytest.raises(ValueError):
        PlanningWake(NOW.replace(tzinfo=None)).to_body()


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        "[]",
        "null",
        "private payload",
        "x" * 257,
        '{"schema":"wrong","requested_at":"2026-01-07T18:00:00Z"}',
        '{"schema":"planning-wake-v1","requested_at":"2026-01-07T18:00:00"}',
        '{"schema":"planning-wake-v1","requested_at":false}',
        '{"schema":"planning-wake-v1","requested_at":"bad","extra":1}',
    ],
)
def test_invalid_wakes_never_touch_bindings(body: object) -> None:
    result = asyncio.run(run_queued_planning(object(), None, body, now=NOW))
    assert result == {"status": "no_action", "detail": "invalid_planning_wake"}


def test_stale_wake_never_creates_a_new_day_of_work() -> None:
    result = asyncio.run(
        run_queued_planning(
            object(), None, PlanningWake(NOW).to_body(), now=NOW + timedelta(days=1)
        )
    )
    assert result == {"status": "no_action", "detail": "stale_planning_wake"}


def test_cron_captures_before_enqueue_without_claiming_or_collecting(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        database = FakeD1()
        await database.exec(D1_SCHEMA)
        order = []

        class OrderedQueue(Queue):
            async def send(self, body: str, *, contentType: str) -> None:
                order.append("enqueue")
                await super().send(body, contentType=contentType)

        async def capture(*args):  # type: ignore[no-untyped-def]
            order.append("capture")

        async def unexpected(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("Cron must not dispatch expensive planning")

        monkeypatch.setattr("sleeper_manager.cloudflare.runtime._capture_forecast", capture)
        monkeypatch.setattr("sleeper_manager.cloudflare.runtime.dispatch_due_work", unexpected)
        env = SimpleNamespace(
            ACKNOWLEDGEMENT_BASE_URL="https://example.test/ack",
            NTFY_TOPIC="test-topic",
            sleeper_manager_state=database,
            PLANNING_QUEUE=OrderedQueue(),
        )
        result = await run_scheduled(env, None, scheduled_at=NOW)
        assert result["status"] == "queued"
        assert order == ["capture", "enqueue"]
        assert await D1StateRepository(database).list_scheduled_work() == ()
        assert PlanningWake.from_body(env.PLANNING_QUEUE.bodies[0]).requested_at == NOW

    asyncio.run(exercise())


def test_enqueue_failure_keeps_work_unclaimed_for_next_cron(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        database = FakeD1()
        await database.exec(D1_SCHEMA)
        repository = D1StateRepository(database)
        await _save_policy(repository)

        class BrokenQueue(Queue):
            async def send(self, body: str, *, contentType: str) -> None:
                raise OSError("queue unavailable")

        env = SimpleNamespace(
            ACKNOWLEDGEMENT_BASE_URL="https://example.test/ack",
            NTFY_TOPIC="test-topic",
            sleeper_manager_state=database,
            PLANNING_QUEUE=BrokenQueue(),
        )
        with pytest.raises(OSError):
            await run_scheduled(env, None, scheduled_at=NOW)
        assert await repository.list_scheduled_work() == ()

    asyncio.run(exercise())


def test_duplicate_queued_wakes_collect_and_deliver_only_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        database = FakeD1()
        await database.exec(D1_SCHEMA)
        repository = D1StateRepository(database)
        await _save_policy(repository)
        sender = RecordingSender()
        monkeypatch.setattr(
            "sleeper_manager.cloudflare.runtime.build_dispatcher",
            lambda *args: NotificationDispatcher(sender),
        )
        collected = []

        async def collect(*args, scheduled_at, hybrid_timeout_seconds, **kwargs):  # type: ignore[no-untyped-def]
            collected.append(scheduled_at)
            assert hybrid_timeout_seconds == 600
            return _assembly(decision_time=scheduled_at)

        async def unexpected(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("Consumer must not repeat capture or watchdog")

        monkeypatch.setattr(
            "sleeper_manager.cloudflare.runtime.collect_cloudflare_planning_inputs", collect
        )
        monkeypatch.setattr("sleeper_manager.cloudflare.runtime._capture_forecast", unexpected)
        monkeypatch.setattr(
            "sleeper_manager.cloudflare.runtime.notify_delayed_daily_planning", unexpected
        )
        env = SimpleNamespace(
            ACKNOWLEDGEMENT_BASE_URL="https://example.test/ack",
            sleeper_manager_state=database,
            PLANNING_QUEUE=Queue(),
            SLEEPER_LEAGUE_ID="league-1",
        )
        body = PlanningWake(NOW - timedelta(minutes=2)).to_body()
        first = await run_queued_planning(env, None, body, now=NOW)
        second = await run_queued_planning(env, None, body, now=NOW)
        assert first["claimed_count"] == 1
        assert second["claimed_count"] == 0
        assert collected == [NOW]
        assert len(sender.messages) == 1
        assert env.PLANNING_QUEUE.bodies == []
        work = await repository.list_scheduled_work(kind=DueWorkKind.DAILY)
        assert work[0].status is ScheduledWorkStatus.COMPLETED
        assert work[0].attempt_count == 1

    asyncio.run(exercise())


def test_failed_collection_remains_durable_for_a_later_queued_wake(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        database = FakeD1()
        await database.exec(D1_SCHEMA)
        repository = D1StateRepository(database)
        await _save_policy(repository)
        sender = RecordingSender()
        monkeypatch.setattr(
            "sleeper_manager.cloudflare.runtime.build_dispatcher",
            lambda *args: NotificationDispatcher(sender),
        )
        calls = []

        async def collect(*args, scheduled_at, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(scheduled_at)
            if len(calls) == 1:
                raise OSError("temporary provider failure")
            return _assembly(decision_time=scheduled_at)

        monkeypatch.setattr(
            "sleeper_manager.cloudflare.runtime.collect_cloudflare_planning_inputs", collect
        )
        env = SimpleNamespace(
            ACKNOWLEDGEMENT_BASE_URL="https://example.test/ack",
            sleeper_manager_state=database,
        )
        first = await run_queued_planning(env, None, PlanningWake(NOW).to_body(), now=NOW)
        work = (await repository.list_scheduled_work(kind=DueWorkKind.DAILY))[0]
        assert first["status"] == "failed"
        assert work.status is ScheduledWorkStatus.RETRY
        later = NOW + timedelta(minutes=5)
        second = await run_queued_planning(env, None, PlanningWake(later).to_body(), now=later)
        assert second["status"] == "success"
        assert calls == [NOW, later]
        assert len(sender.messages) == 1
        assert (await repository.list_scheduled_work(kind=DueWorkKind.DAILY))[0].attempt_count == 2

    asyncio.run(exercise())


def test_queued_planning_persists_actual_completion_time(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        database = FakeD1()
        await database.exec(D1_SCHEMA)
        repository = D1StateRepository(database)
        await _save_policy(repository)
        sender = RecordingSender()
        observed = [NOW]
        monkeypatch.setattr(
            "sleeper_manager.cloudflare.runtime.build_dispatcher",
            lambda *args: NotificationDispatcher(sender),
        )

        async def collect(*args, **kwargs):  # type: ignore[no-untyped-def]
            observed[0] = NOW + timedelta(minutes=2)
            return _assembly(decision_time=observed[0])

        monkeypatch.setattr(
            "sleeper_manager.cloudflare.runtime.collect_cloudflare_planning_inputs", collect
        )
        env = SimpleNamespace(
            ACKNOWLEDGEMENT_BASE_URL="https://example.test/ack",
            sleeper_manager_state=database,
        )
        result = await run_queued_planning(
            env, None, PlanningWake(NOW).to_body(), clock=lambda: observed[0]
        )
        assert result["status"] == "success"
        daily = (await repository.list_scheduled_work(kind=DueWorkKind.DAILY))[0]
        assert daily.updated_at == NOW + timedelta(minutes=2)
        receipt = database.connection.execute(
            "SELECT attempted_at FROM delivery_attempts"
        ).fetchone()
        assert receipt[0] == (NOW + timedelta(minutes=2)).isoformat()

    asyncio.run(exercise())
