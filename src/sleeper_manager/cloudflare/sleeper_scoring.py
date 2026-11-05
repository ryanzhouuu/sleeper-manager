"""Score rostered players from Sleeper's stats so results match the fantasy app.

ESPN still decides when a game is final. Sleeper's foul counts and stat corrections
can differ from ESPN's box score, and Sleeper's numbers are the ones the league uses.
"""

import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sleeper_manager.cloudflare.providers import CloudflareProviderError
from sleeper_manager.domain.nba import (
    DataQualityState,
    GameStatus,
    GameSummary,
    PlayerBoxScore,
    ProviderResult,
    SourceMetadata,
)
from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.integrations.sleeper.game_stats import (
    SleeperGameStatsError,
    box_score_line,
    player_game_stats,
)

_SLEEPER_GAME_DATE_ZONE = ZoneInfo("America/New_York")

SummaryReader = Callable[[str], Awaitable[ProviderResult[GameSummary]]]
PlayerStatsReader = Callable[[str, int], Awaitable[dict[str, Any]]]


class _StatsPending(Exception):
    """Sleeper has not published a usable line for this player yet."""


class SleeperScoredSummarySource:
    """Replace rostered players' ESPN lines, holding the game until Sleeper can score it."""

    def __init__(self, read_summary: SummaryReader, read_stats: PlayerStatsReader) -> None:
        self._read_summary = read_summary
        self._read_stats = read_stats

    async def __call__(
        self, game_id: str, players: Mapping[str, str]
    ) -> ProviderResult[GameSummary]:
        result = await self._read_summary(game_id)
        if result.records.game.status is not GameStatus.FINAL:
            return result
        game_date = result.records.game.start_time.astimezone(_SLEEPER_GAME_DATE_ZONE).date()
        retrieved_at = result.quality.retrieved_at
        boxes: list[PlayerBoxScore] = []
        try:
            for box in result.records.player_box_scores:
                sleeper_id = players.get(box.player_id)
                boxes.append(
                    box
                    if sleeper_id is None
                    else await self._scored_box(box, sleeper_id, game_date, retrieved_at)
                )
        except (CloudflareProviderError, SleeperGameStatsError, _StatsPending) as error:
            warning = f"sleeper_stats_unavailable: {error}"
            print(f"Game {game_id} waiting on Sleeper: {error}", file=sys.stderr)
            return replace(
                result,
                quality=replace(
                    result.quality,
                    state=DataQualityState.PARTIAL,
                    warnings=(*result.quality.warnings, warning),
                ),
            )
        return replace(result, records=replace(result.records, player_box_scores=tuple(boxes)))

    async def _scored_box(
        self,
        box: PlayerBoxScore,
        sleeper_id: str,
        game_date: date,
        retrieved_at: datetime,
    ) -> PlayerBoxScore:
        payload = await self._read_stats(sleeper_id, nba_season_start_year(game_date))
        stats = player_game_stats(payload, game_date)
        if stats is None:
            raise _StatsPending(f"no row for player {sleeper_id} on {game_date}")
        if not stats:
            if box.did_play:
                raise _StatsPending(f"empty stats for player {sleeper_id} on {game_date}")
            return box
        return replace(
            box,
            line=box_score_line(stats),
            additional_sources=(
                *box.additional_sources,
                SourceMetadata("sleeper", f"{sleeper_id}:{game_date}", retrieved_at),
            ),
        )
