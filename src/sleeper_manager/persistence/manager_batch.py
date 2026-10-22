"""Guard every row before auditing a batch; failed validation leaves the whole save untouched."""

import json
from dataclasses import asdict
from datetime import datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from sleeper_manager.persistence.d1 import D1StateRepository
from sleeper_manager.persistence.lock_in_opportunities import lock_in_key_values
from sleeper_manager.persistence.manager_sql import (
    EXPIRE_OLD_TOKENS,
    INSERT_REPLAN,
    REMOVE_LOCK_INDEX,
    SUPERSEDE_DECISIONS,
    UPDATE_DECISION,
)
from sleeper_manager.workflows.manager_decisions import ManagerEdit, reported_decision

_INSERT_BATCH = """
WITH edits AS (SELECT value AS patch FROM json_each(?)),
owned AS (SELECT * FROM lock_in_opportunities
          WHERE league_id=? AND fantasy_week=? AND roster_id=?)
INSERT INTO manager_decision_changes
    (change_id,write_id,league_id,fantasy_week,roster_id,player_id,game_id,action,
     previous_version,previous_state_json,recorded_at)
SELECT patch->>'$.id',patch->>'$.write',o.league_id,o.fantasy_week,o.roster_id,
       o.player_id,o.game_id,patch->>'$.action',o.row_version,patch->>'$.previous',?
FROM edits JOIN owned o ON o.player_id=patch->>'$.player' AND o.game_id=patch->>'$.game'
WHERE NOT EXISTS (
    SELECT 1 FROM edits e LEFT JOIN owned r
      ON r.player_id=e.patch->>'$.player' AND r.game_id=e.patch->>'$.game'
    WHERE r.row_version IS NULL OR r.row_version!=e.patch->>'$.version'
) AND NOT EXISTS (
    SELECT 1 FROM edits e JOIN owned r ON r.status='acknowledged_locked'
    WHERE e.patch->>'$.action'='locked'
      AND (r.player_id=e.patch->>'$.player' OR r.slot_index=e.patch->>'$.slot')
      AND NOT EXISTS (SELECT 1 FROM edits other
                      WHERE r.player_id=other.patch->>'$.player'
                        AND r.game_id=other.patch->>'$.game')
) AND NOT EXISTS (
    SELECT 1 FROM edits a JOIN edits b ON a.patch->>'$.id'!=b.patch->>'$.id'
    WHERE a.patch->>'$.action'='locked' AND b.patch->>'$.action'='locked'
      AND (a.patch->>'$.player'=b.patch->>'$.player'
           OR a.patch->>'$.slot'=b.patch->>'$.slot')
) AND NOT EXISTS (
    SELECT 1 FROM edits e JOIN manager_decision_changes c ON c.change_id=e.patch->>'$.id'
)
"""


async def save_manager_batch(
    repository: D1StateRepository,
    edits: tuple[ManagerEdit, ...],
    *,
    batch_id: str,
    now: datetime,
) -> bool:
    """An exact request replay succeeds without replaying writes or scheduling another plan."""
    if not edits:
        return True
    first = edits[0].current
    scope = lock_in_key_values(first.key)[:3]
    if len(edits) > 16 or any(lock_in_key_values(e.current.key)[:3] != scope for e in edits):
        raise ValueError("Invalid decision batch scope.")
    keys = [(e.current.key.player_id, e.current.key.game_id) for e in edits]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate player-game in decision batch.")
    ids = [sha256(f"{batch_id}:{i}".encode()).hexdigest() for i in range(len(edits))]
    existing = await repository._all(
        "SELECT change_id,league_id,fantasy_week,roster_id,player_id,game_id,action "
        "FROM manager_decision_changes WHERE change_id IN (SELECT value FROM json_each(?))",
        json.dumps(ids),
    )
    if existing:
        found = {str(repository._mapping(r)["change_id"]): repository._mapping(r) for r in existing}
        return len(found) == len(edits) and all(
            identity in found
            and tuple(
                found[identity][n]
                for n in (
                    "league_id",
                    "fantasy_week",
                    "roster_id",
                    "player_id",
                    "game_id",
                    "action",
                )
            )
            == (*lock_in_key_values(edit.current.key), edit.action.value)
            for identity, edit in zip(ids, edits, strict=True)
        )
    updated = tuple(
        reported_decision(e.current, e.action, now, actual_slot=e.actual_slot) for e in edits
    )
    writes = [uuid4().hex for _ in edits]
    payload = json.dumps(
        [
            {
                "id": identity,
                "write": write,
                "player": e.current.key.player_id,
                "game": e.current.key.game_id,
                "action": e.action.value,
                "version": e.current.row_version,
                "slot": result.slot_index,
                "previous": json.dumps(asdict(e.current), default=str),
            }
            for identity, write, e, result in zip(ids, writes, edits, updated, strict=True)
        ]
    )
    stamp = now.isoformat()
    statements = [repository._statement(_INSERT_BATCH, (payload, *scope, stamp))]
    for identity, write, edit, result in zip(ids, writes, edits, updated, strict=True):
        key = edit.current.key
        guard = (identity, write)
        statements.append(
            repository._statement(
                UPDATE_DECISION,
                (
                    result.status.value,
                    result.slot_index,
                    result.slot_position,
                    result.acknowledged_action,
                    result.acknowledged_at.isoformat() if result.acknowledged_at else None,
                    stamp,
                    stamp,
                    *lock_in_key_values(key),
                    edit.current.row_version,
                    *guard,
                ),
            )
        )
        affected = (key.league_id, key.fantasy_week, key.player_id, key.game_id, *guard)
        statements.extend(
            [
                repository._statement(SUPERSEDE_DECISIONS, affected),
                repository._statement(EXPIRE_OLD_TOKENS, (stamp, *affected)),
                repository._statement(REMOVE_LOCK_INDEX, affected),
            ]
        )
    work_key = f"manual:manager:{batch_id}"
    statements.append(
        repository._statement(
            INSERT_REPLAN,
            (
                sha256(work_key.encode()).hexdigest()[:32],
                work_key,
                stamp,
                min(now + timedelta(hours=1), first.fantasy_week_end).isoformat(),
                stamp,
                stamp,
                ids[0],
                writes[0],
            ),
        )
    )
    await repository._database.batch(statements)
    return (
        await repository._first(
            "SELECT 1 FROM manager_decision_changes WHERE change_id=? AND write_id=?",
            ids[0],
            writes[0],
        )
        is not None
    )
