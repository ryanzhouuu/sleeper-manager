import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from sleeper_manager.notifications.base import Notification
from sleeper_manager.notifications.dispatcher import NotificationDispatcher
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.workflows.notification_loop import (
    NotificationLoop,
    RecommendationRequest,
)


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


def test_notification_loop_persists_actions_and_suppresses_duplicates(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
    asyncio.run(repository.initialize())
    sender = RecordingSender()
    workflow = NotificationLoop(
        repository,
        NotificationDispatcher(sender),
        acknowledgement_base_url="https://example.test/ack",
        clock=lambda: NOW,
    )

    first = asyncio.run(workflow.run(request()))
    second = asyncio.run(workflow.run(request()))

    assert first.status == "created"
    assert first.notification is not None
    assert len(first.notification.actions) == 3
    assert first.delivery is not None and first.delivery.succeeded
    assert second.status == "duplicate"
    assert second.notification is None
    assert len(sender.messages) == 1

    acknowledgement_actions = first.notification.actions[:2]
    tokens = {
        parse_qs(urlparse(action.url).query)["token"][0] for action in acknowledgement_actions
    }
    assert len(tokens) == 2
    with (tmp_path / "state.db").open("rb") as file:
        assert all(token.encode() not in file.read() for token in tokens)


def test_notification_loop_retries_after_all_delivery_attempts_fail(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
    asyncio.run(repository.initialize())
    sender = RecordingSender(fail=True)
    workflow = NotificationLoop(
        repository,
        NotificationDispatcher(sender),
        acknowledgement_base_url="https://example.test/ack",
        clock=lambda: NOW,
    )

    first = asyncio.run(workflow.run(request()))
    second = asyncio.run(workflow.run(request()))

    assert first.status == "delivery_failed"
    assert second.status == "delivery_failed"
    assert len(sender.messages) == 2
