import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sleeper_manager.notifications.base import Notification
from sleeper_manager.notifications.dispatcher import NotificationDispatcher
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.workflows.notification_loop import (
    NotificationLoop,
    RecommendationRequest,
)
from sleeper_manager.workflows.plan_rendering import WEEKLY_LINEUP_DECISION_TYPE


class RecordingSender:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[Notification] = []

    async def send(self, notification: Notification) -> None:
        self.messages.append(notification)
        if self.fail:
            raise RuntimeError("delivery failed")


NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)


def request() -> RecommendationRequest:
    return RecommendationRequest(
        league_id="league-1",
        fantasy_week=1,
        player_id="player-1",
        game_id="game-1",
        decision_type="placeholder_lock_in",
        title="Lock player",
        message="Lock the placeholder player",
        deadline=NOW + timedelta(hours=1),
        policy_version="policy-1",
        open_sleeper_url="https://sleeper.com",
    )


def _loop(tmp_path, sender, **kwargs):  # type: ignore[no-untyped-def]
    repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
    asyncio.run(repository.initialize())
    return NotificationLoop(repository, NotificationDispatcher(sender), clock=lambda: NOW, **kwargs)


def test_notification_loop_suppresses_duplicates(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sender = RecordingSender()
    workflow = _loop(tmp_path, sender)

    first = asyncio.run(workflow.run(request()))
    second = asyncio.run(workflow.run(request()))

    assert first.status == "created"
    assert first.notification is not None
    assert first.delivery is not None and first.delivery.succeeded
    assert second.status == "duplicate"
    assert second.notification is None
    assert len(sender.messages) == 1


def test_notification_loop_retries_after_all_delivery_attempts_fail(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sender = RecordingSender(fail=True)
    workflow = _loop(tmp_path, sender)

    first = asyncio.run(workflow.run(request()))
    second = asyncio.run(workflow.run(request()))

    assert first.status == "delivery_failed"
    assert second.status == "delivery_failed"
    assert len(sender.messages) == 2
    connection = sqlite3.connect(tmp_path / "state.db")
    try:
        attempts = connection.execute(
            """
            SELECT attempt_number FROM delivery_attempts
            WHERE provider != '_delivery_claim'
            ORDER BY attempt_number
            """
        ).fetchall()
    finally:
        connection.close()
    assert attempts == [(1,), (2,)]


def test_lock_in_notification_has_no_acknowledgement_actions(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sender = RecordingSender()
    workflow = _loop(tmp_path, sender)

    result = asyncio.run(workflow.run(request()))

    assert result.notification is not None
    assert result.notification.actions == ()
    connection = sqlite3.connect(tmp_path / "state.db")
    try:
        (token_rows,) = connection.execute("SELECT COUNT(*) FROM action_tokens").fetchone()
    finally:
        connection.close()
    assert token_rows == 0


def test_notifications_link_only_to_the_dashboard(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sender = RecordingSender()
    workflow = _loop(tmp_path, sender, dashboard_url="https://dashboard.test/")

    for decision_type in ("placeholder_lock_in", WEEKLY_LINEUP_DECISION_TYPE):
        result = asyncio.run(workflow.run(replace(request(), decision_type=decision_type)))
        assert result.notification is not None
        (action,) = result.notification.actions
        assert (action.label, action.url) == ("Open dashboard", "https://dashboard.test/")
        assert "/ack" not in action.url


def test_concurrent_identical_runs_send_one_notification(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class RaceSender:
        def __init__(self) -> None:
            self.messages: list[Notification] = []
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def send(self, notification: Notification) -> None:
            self.entered.set()
            await self.release.wait()
            self.messages.append(notification)

    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        sender = RaceSender()
        workflow = NotificationLoop(
            repository,
            NotificationDispatcher(sender),
            clock=lambda: NOW,
        )
        first_task = asyncio.create_task(workflow.run(request()))
        await sender.entered.wait()
        second_task = asyncio.create_task(workflow.run(request()))
        await asyncio.sleep(0)
        sender.release.set()
        first, second = await asyncio.gather(first_task, second_task)
        statuses = sorted([first.status, second.status])
        assert statuses == ["created", "duplicate"]
        assert len(sender.messages) == 1

    asyncio.run(exercise())
