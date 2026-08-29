"""Shared NBA evidence fixtures for historical team-week projection tests."""

from __future__ import annotations

from datetime import UTC, datetime

from team_week_source_support import RETRIEVED_AT

from sleeper_manager.backtesting.experiments.data import (
    HistoricalExperimentInputs,
    SourceArtifact,
)
from sleeper_manager.domain.nba import (
    GameStatus,
    PlayerBoxScore,
    ProviderPlayer,
    ScheduledGame,
    SourceMetadata,
)
from sleeper_manager.domain.scoring import BoxScoreLine


def _nba_inputs(
    *, same_day_points: int = 1, include_bench: bool = False
) -> HistoricalExperimentInputs:
    """Build NBA evidence with one prior-day and one same-day historical outcome."""

    source = _source("fixture")
    games = (
        ScheduledGame(
            "prior",
            datetime(2026, 1, 31, 20, tzinfo=UTC),
            GameStatus.FINAL,
            "CHI",
            "BOS",
            None,
            source,
        ),
        ScheduledGame(
            "same-day",
            datetime(2026, 2, 2, 15, tzinfo=UTC),
            GameStatus.FINAL,
            "CHI",
            "BOS",
            None,
            source,
        ),
        ScheduledGame(
            "target",
            datetime(2026, 2, 2, 20, tzinfo=UTC),
            GameStatus.FINAL,
            "CHI",
            "BOS",
            None,
            source,
        ),
    )
    boxes = [
        _box("prior", datetime(2026, 1, 31, 20, tzinfo=UTC), 10),
        _box("same-day", datetime(2026, 2, 2, 15, tzinfo=UTC), same_day_points),
        _box("target", datetime(2026, 2, 2, 20, tzinfo=UTC), 20),
        _box("target", datetime(2026, 2, 2, 20, tzinfo=UTC), 15, "unrelated-player"),
    ]
    if include_bench:
        boxes.extend(
            (
                _box("prior", datetime(2026, 1, 31, 20, tzinfo=UTC), 8, "provider-2"),
                _box("target", datetime(2026, 2, 2, 20, tzinfo=UTC), 12, "provider-2"),
            )
        )
    return HistoricalExperimentInputs(
        games=games,
        player_box_scores=boxes,
        team_box_scores=(),
        teams=(),
        provider_players=(
            ProviderPlayer("provider-1", "Fixture Guard", "CHI", "CHI", True, source),
            *(
                (ProviderPlayer("provider-2", "Fixture Bench", "CHI", "CHI", True, source),)
                if include_bench
                else ()
            ),
        ),
        artifacts=(
            SourceArtifact(2026, "fixture", "fixture.rds", "fixture", "hash", 1, len(boxes)),
        ),
        excluded_player_rows=0,
    )


def _box(
    game_id: str,
    played_at: datetime,
    points: int,
    player_id: str = "provider-1",
) -> PlayerBoxScore:
    """Create a finalized provider box score for the one rostered fixture player."""

    return PlayerBoxScore(
        game_id,
        player_id,
        "CHI",
        played_at,
        True,
        True,
        30,
        BoxScoreLine(points=points),
        _source(f"{game_id}:{player_id}"),
    )


def _source(provider_id: str) -> SourceMetadata:
    """Return fixture provenance at the shared retrieval time."""

    return SourceMetadata("fixture", provider_id, RETRIEVED_AT)
