"""Deterministic full-field outcomes and independently declared fixture opportunities."""

from datetime import UTC, datetime, timedelta

from sleeper_manager.domain.scoring import BoxScoreLine
from sleeper_manager.projections.hybrid_config import OUTCOME_FIELDS
from sleeper_manager.projections.hybrid_types import (
    HybridHistory,
    JointGameObservation,
    ParticipationOpportunity,
)

CUTOFF = datetime(2026, 10, 20, 12, tzinfo=UTC)
TIPOFF = CUTOFF + timedelta(hours=12)


def game(
    player: str, index: int, *, points: int = 20, start: datetime | None = None
) -> JointGameObservation:
    at = start or datetime(2026, 3, 1, tzinfo=UTC) + timedelta(days=index)
    return JointGameObservation(
        player,
        f"{player}-{index}",
        at,
        at + timedelta(hours=3),
        True,
        30,
        BoxScoreLine(
            points=points,
            rebounds=5 + index % 7,
            assists=2 + index % 6,
            steals=index % 3,
            blocks=index % 2,
            turnovers=index % 4,
            three_pointers_made=index % 4,
        ),
        OUTCOME_FIELDS,
        "outcomes-v1",
    )


def history(*, include_player: bool = True) -> HybridHistory:
    players = (["target"] if include_player else []) + [f"donor-{i}" for i in range(6)]
    rows = tuple(game(player, i, points=15 + i % 20) for player in players for i in range(30))
    opportunities = tuple(
        ParticipationOpportunity(
            row.player_id, row.game_id, row.game_start, row.finalized_at, row.did_play, "census-v1"
        )
        for row in rows
    )
    return HybridHistory("dataset-v1", rows, opportunities)
