"""Deliver one no-change daily result independently of material advice revisions."""

from datetime import datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

from sleeper_manager.persistence.base import RecommendationRecord
from sleeper_manager.workflows.notification_loop import (
    DAILY_PLAN_COMPLETED_KIND,
    NotificationLoop,
    NotificationLoopResult,
    RecommendationRequest,
)


async def notify_daily_completion(
    recommendation: RecommendationRecord,
    notifications: NotificationLoop,
    *,
    now: datetime,
    local_timezone: tzinfo | None,
    open_sleeper_url: str,
) -> NotificationLoopResult | None:
    """Keep original warnings; deduplicate by manager-local day and retry failed deliveries."""
    timezone = local_timezone or ZoneInfo("America/Chicago")
    local_day = now.astimezone(timezone).date()
    day_end = datetime.combine(local_day + timedelta(days=1), time.min, timezone)
    deadline = min(recommendation.deadline or day_end, day_end)
    if deadline <= now:
        return None
    return await notifications.run(
        RecommendationRequest(
            league_id=recommendation.league_id,
            fantasy_week=recommendation.fantasy_week,
            player_id=recommendation.player_id,
            game_id=None,
            decision_type=DAILY_PLAN_COMPLETED_KIND,
            title=recommendation.title,
            message=recommendation.message,
            deadline=deadline,
            policy_version=recommendation.policy_version,
            open_sleeper_url=open_sleeper_url,
            trace_json=recommendation.trace_json,
            idempotency_key=f"daily-plan-completed:{recommendation.league_id}:{local_day.isoformat()}",
        )
    )
