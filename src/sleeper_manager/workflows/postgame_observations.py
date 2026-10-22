"""Qualify and fingerprint direct provider observations before they advance score state."""

import json
from dataclasses import asdict
from hashlib import sha256

from sleeper_manager.domain.nba import DataQualityState, GameStatus, GameSummary, ProviderResult
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityRecord


def summary_wait_reason(
    result: ProviderResult[GameSummary],
    opportunities: tuple[LockInOpportunityRecord, ...],
) -> str | None:
    """Identify provider evidence that cannot count as a stable final poll."""

    if result.records.game.status is not GameStatus.FINAL:
        return "game_not_final"
    if result.quality.state in {
        DataQualityState.PARTIAL,
        DataQualityState.EMPTY,
        DataQualityState.ERROR,
        DataQualityState.UNRESOLVED,
        DataQualityState.STALE,
    }:
        return "summary_incomplete"
    present = {item.player_id for item in result.records.player_box_scores}
    if any(item.provider_player_id not in present for item in opportunities):
        return "rostered_player_missing"
    return None


def summary_fingerprint(summary: GameSummary) -> str:
    """Hash normalized game and box-score facts without retrieval timestamps."""

    payload = {
        "game": {
            "id": summary.game.provider_id,
            "start": summary.game.start_time.isoformat(),
            "status": summary.game.status.value,
            "completed_periods": summary.game.completed_periods,
        },
        "players": [
            {
                "player_id": item.player_id,
                "team_id": item.team_id,
                "started": item.started,
                "did_play": item.did_play,
                "minutes": item.minutes,
                "line": asdict(item.line),
            }
            for item in sorted(summary.player_box_scores, key=lambda value: value.player_id)
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()
