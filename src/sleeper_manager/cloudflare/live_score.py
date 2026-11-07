"""Score a matchup side the way Sleeper's lock-in mode does: only locked games count."""

from dataclasses import dataclass

from sleeper_manager.cloudflare.sleeper_scoring import PlayerStatsReader
from sleeper_manager.domain.scoring import ScoringPolicy, calculate_fantasy_points
from sleeper_manager.integrations.sleeper.game_stats import box_score_line, game_stats_by_id
from sleeper_manager.integrations.sleeper.matchup_legs import MatchupLeg


@dataclass(frozen=True, slots=True)
class SideScore:
    """``pending`` means a locked game's stats are not posted yet, so ``points`` is unknown."""

    points: float | None
    final: bool
    locked: int
    starters: int
    pending: bool


async def side_score(
    leg: MatchupLeg, *, read_stats: PlayerStatsReader, season: int, scoring: ScoringPolicy
) -> SideScore:
    locked = len(leg.starters_games)
    starters = len(leg.starters)
    if leg.points is not None:
        return SideScore(leg.points, True, locked, starters, False)
    total = 0.0
    for player_id, game_id in leg.starters_games.items():
        stats = game_stats_by_id(await read_stats(player_id, season), game_id)
        if not stats:
            return SideScore(None, False, locked, starters, True)
        total += calculate_fantasy_points(box_score_line(stats), scoring)
    return SideScore(round(total, 2), False, locked, starters, False)
