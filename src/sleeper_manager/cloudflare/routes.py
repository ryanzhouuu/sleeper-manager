from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sleeper_manager.persistence.base import (
    AcknowledgementAction,
    AcknowledgementOutcome,
    AsyncStateRepository,
)
from sleeper_manager.persistence.tokens import hash_action_token


@dataclass(frozen=True, slots=True)
class RouteResponse:
    status_code: int
    payload: dict[str, Any]


def parse_action(value: str | None) -> AcknowledgementAction | None:
    if value is None:
        return None
    normalized = value.strip().casefold()
    if normalized in {"locked", "lock"}:
        return AcknowledgementAction.LOCKED
    if normalized in {"passed", "pass"}:
        return AcknowledgementAction.PASSED
    return None


async def acknowledge(
    repository: AsyncStateRepository,
    values: dict[str, str],
    *,
    now: datetime | None = None,
) -> RouteResponse:
    raw_token = values.get("token", "")
    action = parse_action(values.get("action"))
    if not raw_token or len(raw_token) > 256 or action is None:
        return RouteResponse(400, {"status": "invalid_request"})

    result = await repository.consume_action_token(
        hash_action_token(raw_token),
        action,
        now or datetime.now(UTC),
    )
    if result.outcome is AcknowledgementOutcome.APPLIED:
        return RouteResponse(
            200,
            {
                "status": "acknowledged",
                "action": action.value,
                "recommendation_id": (
                    result.recommendation.recommendation_id
                    if result.recommendation is not None
                    else None
                ),
            },
        )
    if result.outcome is AcknowledgementOutcome.ALREADY_USED:
        return RouteResponse(200, {"status": "already_used"})
    if result.outcome is AcknowledgementOutcome.EXPIRED:
        return RouteResponse(410, {"status": "expired"})
    if result.outcome is AcknowledgementOutcome.CONFLICT:
        return RouteResponse(409, {"status": "conflict"})
    return RouteResponse(404, {"status": "invalid"})
