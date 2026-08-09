import asyncio
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from sleeper_manager.cloudflare.routes import acknowledge
from sleeper_manager.notifications.base import Notification
from sleeper_manager.notifications.dispatcher import NotificationDispatcher
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.workflows.notification_loop import NotificationLoop, RecommendationRequest

NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)


class RecordingSender:
    async def send(self, notification: Notification) -> None:
        return None


def create_notification(tmp_path):  # type: ignore[no-untyped-def]
    repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
    asyncio.run(repository.initialize())
    workflow = NotificationLoop(
        repository,
        NotificationDispatcher(RecordingSender()),
        acknowledgement_base_url="https://example.test/ack",
        clock=lambda: NOW,
    )
    result = asyncio.run(
        workflow.run(
            RecommendationRequest(
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
        )
    )
    return repository, result


def test_acknowledge_route_applies_and_replays_once(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository, result = create_notification(tmp_path)
    assert result.notification is not None
    values = parse_qs(urlparse(result.notification.actions[0].url).query)
    request = {"token": values["token"][0], "action": "lock"}

    response = asyncio.run(acknowledge(repository, request, now=NOW + timedelta(minutes=1)))
    replay = asyncio.run(acknowledge(repository, request, now=NOW + timedelta(minutes=2)))

    assert response.status_code == 200
    assert json.loads(json.dumps(response.payload))["status"] == "acknowledged"
    assert replay.status_code == 200
    assert replay.payload == {"status": "already_used"}
    assert asyncio.run(repository.is_locked(result.recommendation.recommendation_id))


def test_acknowledge_route_rejects_invalid_request(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository, _ = create_notification(tmp_path)

    response = asyncio.run(acknowledge(repository, {"token": "bad", "action": "delete"}))

    assert response.status_code == 400
    assert response.payload == {"status": "invalid_request"}
