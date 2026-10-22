"""Persisted unfinished work is detected before a later invocation repeats expensive collection."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from sleeper_manager.cloudflare.runtime import run_scheduled
from sleeper_manager.notifications.dispatcher import NotificationDispatcher
from sleeper_manager.persistence.d1 import D1_SCHEMA, D1StateRepository
from tests.sleeper_manager.cloudflare.test_scheduled_dispatcher import (
    SEVEN_AM_CENTRAL,
    _save_policy,
)
from tests.sleeper_manager.persistence.test_d1 import FakeD1
from tests.sleeper_manager.workflows.test_notification_loop import RecordingSender


def test_watchdog_alert_precedes_repeated_termination(monkeypatch: pytest.MonkeyPatch) -> None:
    class Terminated(BaseException):
        pass

    async def exercise() -> None:
        database = FakeD1()
        await database.exec(D1_SCHEMA)
        repository = D1StateRepository(database)
        await _save_policy(repository, at=SEVEN_AM_CENTRAL)
        sender = RecordingSender()
        monkeypatch.setattr(
            "sleeper_manager.cloudflare.runtime.build_dispatcher",
            lambda env, fetcher: NotificationDispatcher(sender),
        )
        calls = []

        async def terminate(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(1)
            assert len(sender.messages) == (1 if len(calls) >= 2 else 0)
            raise Terminated()

        monkeypatch.setattr(
            "sleeper_manager.cloudflare.runtime.collect_cloudflare_planning_inputs", terminate
        )
        env = SimpleNamespace(
            ACKNOWLEDGEMENT_BASE_URL="https://example.test/ack",
            SLEEPER_LEAGUE_ID="league-1",
            sleeper_manager_state=database,
        )
        for index in range(4):
            with pytest.raises(Terminated):
                await run_scheduled(
                    env,
                    None,
                    scheduled_at=SEVEN_AM_CENTRAL + timedelta(minutes=15 * index),
                    correlation_id=f"wake-{index}",
                )
        assert len(sender.messages) == 1
        assert len(calls) == 4

    asyncio.run(exercise())
