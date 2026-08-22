from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Literal, Protocol
from urllib.parse import urlencode

from sleeper_manager.notifications.base import Notification, NotificationAction
from sleeper_manager.notifications.dispatcher import (
    NotificationDeliveryResult,
    NotificationDispatcher,
)
from sleeper_manager.persistence.base import (
    AcknowledgementAction,
    ActionTokenRecord,
    AsyncStateRepository,
    DeliveryAttemptRecord,
    RecommendationRecord,
)
from sleeper_manager.persistence.tokens import generate_action_token, hash_action_token
from sleeper_manager.workflows.plan_rendering import WEEKLY_LINEUP_DECISION_TYPE

# Frozen product decision: lineup notifications never carry acknowledgement actions,
# regardless of caller configuration.
_UNACKNOWLEDGEABLE_KINDS = frozenset({WEEKLY_LINEUP_DECISION_TYPE})


class Clock(Protocol):
    def __call__(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class RecommendationRequest:
    league_id: str
    fantasy_week: int
    player_id: str
    game_id: str | None
    decision_type: str
    title: str
    message: str
    deadline: datetime
    policy_version: str
    open_sleeper_url: str
    trace_json: str = "{}"
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class NotificationLoopResult:
    status: Literal["created", "duplicate", "delivery_failed"]
    recommendation: RecommendationRecord
    notification: Notification | None
    delivery: NotificationDeliveryResult | None


class NotificationLoop:
    def __init__(
        self,
        repository: AsyncStateRepository,
        dispatcher: NotificationDispatcher,
        *,
        acknowledgement_base_url: str,
        clock: Clock | None = None,
        acknowledgement_kinds: frozenset[str] | None = None,
    ) -> None:
        if not acknowledgement_base_url:
            raise ValueError("acknowledgement base URL is required")
        self._repository = repository
        self._dispatcher = dispatcher
        self._acknowledgement_base_url = acknowledgement_base_url.rstrip("?")
        # None issues acknowledgements for every kind: an
        # explicit set restricts them to those kinds, so an empty set disables them.
        self._acknowledgement_kinds = acknowledgement_kinds
        self._clock = clock or (lambda: datetime.now(UTC))

    def _action_url(self, token: str, action: AcknowledgementAction) -> str:
        query = urlencode({"token": token, "action": action.value})
        separator = "&" if "?" in self._acknowledgement_base_url else "?"
        return f"{self._acknowledgement_base_url}{separator}{query}"

    @staticmethod
    def _recommendation_id(idempotency_key: str) -> str:
        return sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]

    async def run(self, request: RecommendationRequest) -> NotificationLoopResult:
        now = self._clock()
        if request.deadline <= now:
            raise ValueError("recommendation deadline must be in the future")
        await self._repository.expire_recommendations(now)
        idempotency_key = request.idempotency_key or self._default_idempotency_key(request)
        recommendation = RecommendationRecord(
            recommendation_id=self._recommendation_id(idempotency_key),
            idempotency_key=idempotency_key,
            league_id=request.league_id,
            fantasy_week=request.fantasy_week,
            player_id=request.player_id,
            game_id=request.game_id,
            decision_type=request.decision_type,
            title=request.title,
            message=request.message,
            deadline=request.deadline,
            policy_version=request.policy_version,
            created_at=now,
            trace_json=request.trace_json,
        )
        created = await self._repository.create_recommendation(recommendation)
        if not created:
            existing = await self._repository.get_recommendation(recommendation.recommendation_id)
            if existing is None:
                return NotificationLoopResult("duplicate", recommendation, None, None)
            recommendation = existing
            if await self._repository.has_successful_delivery(recommendation.recommendation_id):
                return NotificationLoopResult("duplicate", recommendation, None, None)
            if recommendation.deadline is not None and recommendation.deadline <= now:
                return NotificationLoopResult("duplicate", recommendation, None, None)

        return await self._deliver(recommendation, request, now)

    async def _deliver(
        self,
        recommendation: RecommendationRecord,
        request: RecommendationRequest,
        now: datetime,
    ) -> NotificationLoopResult:

        requires_acknowledgements = request.decision_type not in _UNACKNOWLEDGEABLE_KINDS and (
            self._acknowledgement_kinds is None
            or request.decision_type in self._acknowledgement_kinds
        )
        actions: list[NotificationAction] = []
        if requires_acknowledgements:
            for acknowledgement_action, label in (
                (AcknowledgementAction.LOCKED, "Locked"),
                (AcknowledgementAction.PASSED, "Passed"),
            ):
                raw_token = generate_action_token()
                await self._repository.create_action_token(
                    ActionTokenRecord(
                        token_hash=hash_action_token(raw_token),
                        recommendation_id=recommendation.recommendation_id,
                        action=acknowledgement_action,
                        created_at=now,
                        expires_at=request.deadline,
                    )
                )
                actions.append(
                    NotificationAction(
                        label=label,
                        url=self._action_url(raw_token, acknowledgement_action),
                        method="POST",
                    )
                )
        actions.append(NotificationAction(label="Open Sleeper", url=request.open_sleeper_url))

        notification = Notification(
            title=request.title,
            message=request.message,
            priority=4,
            tags=("basketball", "warning"),
            actions=tuple(actions),
        )
        delivery = await self._dispatcher.send_with_result(notification)
        for index, attempt in enumerate(delivery.attempts, start=1):
            delivery_id = sha256(
                f"{recommendation.recommendation_id}:{index}:{attempt.provider}".encode()
            ).hexdigest()
            await self._repository.record_delivery_attempt(
                DeliveryAttemptRecord(
                    delivery_id=delivery_id,
                    recommendation_id=recommendation.recommendation_id,
                    provider=attempt.provider,
                    attempt_number=index,
                    attempted_at=self._clock(),
                    succeeded=attempt.succeeded,
                    error=attempt.error,
                )
            )
        status: Literal["created", "delivery_failed"] = (
            "created" if delivery.succeeded else "delivery_failed"
        )
        return NotificationLoopResult(status, recommendation, notification, delivery)

    @staticmethod
    def _default_idempotency_key(request: RecommendationRequest) -> str:
        game = request.game_id or "none"
        return ":".join(
            (
                request.league_id,
                str(request.fantasy_week),
                request.player_id,
                game,
                request.decision_type,
                request.policy_version,
            )
        )


def default_placeholder_request(
    *,
    league_id: str,
    now: datetime,
    fantasy_week: int = 1,
    player_id: str = "phase3-placeholder-player",
    game_id: str = "phase3-placeholder-game",
    open_sleeper_url: str = "https://sleeper.com",
) -> RecommendationRequest:
    deadline = now + timedelta(hours=1)
    return RecommendationRequest(
        league_id=league_id,
        fantasy_week=fantasy_week,
        player_id=player_id,
        game_id=game_id,
        decision_type="placeholder_lock_in",
        title="Phase 3 notification test",
        message=(
            "Confirm the placeholder action only after completing the corresponding action "
            "in Sleeper."
        ),
        deadline=deadline,
        policy_version="phase3-placeholder-v1",
        open_sleeper_url=open_sleeper_url,
    )
