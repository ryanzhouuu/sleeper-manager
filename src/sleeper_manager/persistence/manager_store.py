"""Audited owner edits invalidate old advice and create durable replans in one D1 batch."""

import json
from dataclasses import asdict
from datetime import datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from sleeper_manager.persistence.d1 import D1StateRepository
from sleeper_manager.persistence.lock_in_opportunities import (
    LockInOpportunityRecord,
    lock_in_key_values,
)
from sleeper_manager.persistence.manager_sql import (
    EXPIRE_OLD_TOKENS,
    INSERT_CHANGE,
    INSERT_REPLAN,
    REMOVE_LOCK_INDEX,
    SUPERSEDE_DECISIONS,
    UPDATE_DECISION,
)
from sleeper_manager.workflows.manager_decisions import ManagerAction, reported_decision


class D1ManagerStore(D1StateRepository):
    """Reuse D1 decoding without expanding the scheduler repository's interface."""

    async def preview_token(self, token_hash: str) -> dict[str, object] | None:
        """Read a scoped token and its message without consuming it."""
        return await self._first(
            "SELECT t.action,t.expires_at,t.used_at,r.recommendation_id,r.title,r.message,r.status "
            "FROM action_tokens t JOIN recommendations r USING (recommendation_id) "
            "WHERE t.token_hash=?",
            token_hash,
        )

    async def has_pending_manager_plan(self, now: datetime) -> bool:
        """Hide stale advice while a saved owner decision is awaiting its replan."""
        return (
            await self._first(
                "SELECT 1 FROM scheduled_work WHERE (dedupe_key LIKE 'manual:manager:%' "
                "OR dedupe_key LIKE 'manual:ack:%') AND status IN ('pending','running','retry') "
                "AND deadline>? LIMIT 1",
                now.isoformat(),
            )
            is not None
        )

    async def save_decision(
        self,
        current: LockInOpportunityRecord,
        action: ManagerAction,
        *,
        change_id: str,
        now: datetime,
        actual_slot: tuple[int, str] | None,
    ) -> bool:
        """CAS failure writes nothing; an identical request ID replays without another replan."""
        key = current.key
        existing = await self._first(
            "SELECT action,league_id,fantasy_week,roster_id,player_id,game_id "
            "FROM manager_decision_changes WHERE change_id=?",
            change_id,
        )
        if existing is not None:
            return bool(
                existing["action"] == action.value
                and tuple(
                    existing[name]
                    for name in ("league_id", "fantasy_week", "roster_id", "player_id", "game_id")
                )
                == tuple(lock_in_key_values(key))
            )
        updated = reported_decision(current, action, now, actual_slot=actual_slot)
        stamp = now.isoformat()
        write_id = uuid4().hex
        work_key = f"manual:manager:{change_id}"
        statements = [
            self._statement(
                INSERT_CHANGE,
                (
                    change_id,
                    write_id,
                    action.value,
                    json.dumps(asdict(current), default=str),
                    stamp,
                    *lock_in_key_values(key),
                    current.row_version,
                    action.value,
                    updated.slot_index,
                ),
            ),
            self._statement(
                UPDATE_DECISION,
                (
                    updated.status.value,
                    updated.slot_index,
                    updated.slot_position,
                    updated.acknowledged_action,
                    updated.acknowledged_at.isoformat() if updated.acknowledged_at else None,
                    stamp,
                    stamp,
                    *lock_in_key_values(key),
                    current.row_version,
                    change_id,
                    write_id,
                ),
            ),
            self._statement(
                SUPERSEDE_DECISIONS,
                (
                    key.league_id,
                    key.fantasy_week,
                    key.player_id,
                    key.game_id,
                    change_id,
                    write_id,
                ),
            ),
            self._statement(
                EXPIRE_OLD_TOKENS,
                (
                    stamp,
                    key.league_id,
                    key.fantasy_week,
                    key.player_id,
                    key.game_id,
                    change_id,
                    write_id,
                ),
            ),
            self._statement(
                REMOVE_LOCK_INDEX,
                (
                    key.league_id,
                    key.fantasy_week,
                    key.player_id,
                    key.game_id,
                    change_id,
                    write_id,
                ),
            ),
            self._statement(
                INSERT_REPLAN,
                (
                    sha256(work_key.encode()).hexdigest()[:32],
                    work_key,
                    stamp,
                    min(now + timedelta(hours=1), current.fantasy_week_end).isoformat(),
                    stamp,
                    stamp,
                    change_id,
                    write_id,
                ),
            ),
        ]
        results = await self._database.batch(statements)
        return self._changes(results[0]) == 1

    async def decision_history(
        self, league_id: str, week: int, roster_id: int
    ) -> list[dict[str, object]]:
        """Return bounded owner-only history; earlier acknowledgements stay in their tables."""
        rows = await self._all(
            "SELECT player_id,game_id,action,recorded_at,previous_state_json "
            "FROM manager_decision_changes WHERE league_id=? AND fantasy_week=? AND roster_id=? "
            "ORDER BY recorded_at DESC LIMIT 50",
            league_id,
            week,
            roster_id,
        )
        return [dict(self._mapping(row)) for row in rows]

    async def ensure_ack_replan(self, recommendation_id: str, now: datetime) -> None:
        """Replay after an ambiguous acknowledgement write also repairs its missing wake."""
        key = f"manual:ack:{recommendation_id}"
        await self._run(
            "INSERT OR IGNORE INTO scheduled_work "
            "(work_id,dedupe_key,kind,due_at,status,deadline,created_at,updated_at) "
            "VALUES (?,?,'daily',?,'pending',?,?,?)",
            sha256(key.encode()).hexdigest()[:32],
            key,
            now.isoformat(),
            (now + timedelta(hours=1)).isoformat(),
            now.isoformat(),
            now.isoformat(),
        )
