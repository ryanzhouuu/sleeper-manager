"""Project owner-only editor evidence into the document consumed by the Next.js UI."""

import json
from datetime import datetime
from uuid import uuid4

from sleeper_manager.cloudflare.home_document import player_image_url, team_image_url
from sleeper_manager.cloudflare.manager_context import ManagerContext
from sleeper_manager.cloudflare.manager_games import (
    lockable_games,
    recommendation_for,
    recorded_state,
)
from sleeper_manager.persistence.base import RecommendationRecord
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityRecord


def editor_document(
    context: ManagerContext,
    records: tuple[LockInOpportunityRecord, ...],
    history: list[dict[str, object]],
    csrf: str,
    *,
    now: datetime,
    pending: bool,
    plan: RecommendationRecord | None,
) -> dict[str, object]:
    """Keep private row versions and CSRF proofs out of the public home document."""
    active = lockable_games(records, context, now)
    saved = tuple(row for row in records if row.acknowledged_action)
    visible = {row.key for row in (*active, *saved)}
    advice = None if pending else plan

    def card(row: LockInOpportunityRecord) -> dict[str, object]:
        """Use the same eligibility and advice labels as the legacy editor."""
        identity = context.identities.get(row.key.player_id)
        slot = context.actual_slot(row.key.player_id)
        return {
            "player_id": row.key.player_id,
            "game_id": row.key.game_id,
            "version": row.row_version,
            "name": context.names.get(row.key.player_id, row.key.player_id),
            "placement": slot[1] if slot else "Bench / reserve",
            "start": row.scheduled_start.isoformat(),
            "deadline": row.action_deadline.isoformat(),
            "score": row.stable_score,
            "action": row.acknowledged_action or "not_locked",
            "advisor": recommendation_for(row, advice, now),
            "saved": recorded_state(row, now),
            "portrait_url": player_image_url(identity.espn_id) if identity else None,
            "team": identity.nba_team if identity else None,
            "team_image_url": team_image_url(identity.nba_team) if identity else None,
        }

    return {
        "status": "ready",
        "week": context.week,
        "timezone": context.timezone,
        "csrf": csrf,
        "batch_id": uuid4().hex,
        "request_id": uuid4().hex,
        "pending": pending,
        "plan": {"title": plan.title, "message": plan.message} if plan and not pending else None,
        "active": [card(row) for row in active],
        "saved": [card(row) for row in saved],
        "past": [
            {
                "name": context.names.get(row.key.player_id, row.key.player_id),
                "start": row.scheduled_start.isoformat(),
                "score": row.stable_score,
                "state": recorded_state(row, now, context=context, records=records),
            }
            for row in records
            if row.key not in visible and row.stable_score is not None
        ],
        "history": [
            {
                "name": context.names.get(str(row["player_id"]), str(row["player_id"])),
                "game_id": row["game_id"],
                "previous": json.loads(str(row["previous_state_json"])).get("acknowledged_action")
                or "not_locked",
                "action": row["action"],
                "recorded_at": row["recorded_at"],
            }
            for row in history
        ],
    }
