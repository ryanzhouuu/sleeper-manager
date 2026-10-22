"""Validate the complete shared form before applying any selected game changes."""

from dataclasses import replace
from datetime import datetime
from uuid import UUID

from sleeper_manager.cloudflare.manager_context import ManagerContext
from sleeper_manager.persistence.manager_store import D1ManagerStore
from sleeper_manager.workflows.manager_decisions import ManagerAction, ManagerEdit


async def save_manager_form(
    values: dict[str, str], store: D1ManagerStore, context: ManagerContext, now: datetime
) -> tuple[bool, bool]:
    """Return (saved, changed); unchanged forms create no audit rows or planning work."""
    batch_id = UUID(values["batch_id"]).hex
    count = int(values["count"])
    if not 1 <= count <= 16:
        raise ValueError("Invalid game count.")
    records = await store.list_week_lock_in_opportunities(context.league_id, context.week)
    owned = {
        (r.key.player_id, r.key.game_id): r for r in records if r.key.roster_id == context.roster_id
    }
    edits = []
    seen = set()
    for i in range(count):
        key = (values[f"player_id_{i}"], values[f"game_id_{i}"])
        if key not in owned or key in seen:
            raise ValueError("Game unavailable. Reload the owner controls.")
        seen.add(key)
        current = owned[key]
        version = int(values[f"version_{i}"])
        action = ManagerAction(values[f"action_{i}"])
        if version != current.row_version:
            return False, False
        if action.value != (current.acknowledged_action or "not_locked"):
            edits.append(
                ManagerEdit(
                    replace(current, row_version=version), action, context.actual_slot(key[0])
                )
            )
    saved = await store.save_decisions(tuple(edits), batch_id=batch_id, now=now)
    return saved, bool(edits)
