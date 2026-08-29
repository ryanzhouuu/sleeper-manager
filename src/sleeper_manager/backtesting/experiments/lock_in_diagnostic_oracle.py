"""Validate temporal feasibility of historical Lock-In oracle selections."""

from __future__ import annotations

from datetime import datetime

from sleeper_manager.backtesting.experiments.lock_in_diagnostic_models import (
    OracleFeasibilityCheck,
)
from sleeper_manager.backtesting.replay.inputs.models import HistoricalTeamWeekInput
from sleeper_manager.backtesting.replay.models import (
    ReplayGame,
    ReplayPlayerGame,
    TeamWeekReplayResult,
)


def oracle_feasibility_checks(
    oracle: TeamWeekReplayResult,
    team_week: HistoricalTeamWeekInput,
) -> tuple[OracleFeasibilityCheck, ...]:
    """Check whether each oracle selection could have been locked in time."""

    game_by_id = {game.game_id: game for game in team_week.games}
    player_games = team_week.player_games
    checks: list[OracleFeasibilityCheck] = []
    for decision in oracle.decisions:
        if decision.game_id is None or decision.slot_index is None:
            continue
        player_game = next(
            item
            for item in player_games
            if item.sleeper_id == decision.player_id and item.game_id == decision.game_id
        )
        if _is_final_rostered_game(player_game, player_games, game_by_id):
            checks.append(
                OracleFeasibilityCheck(
                    sleeper_id=decision.player_id,
                    game_id=decision.game_id,
                    slot_index=decision.slot_index,
                    kind="automatic_final",
                    feasible=True,
                    detail="Selected final rostered game is treated as automatic.",
                )
            )
            continue
        game = game_by_id[decision.game_id]
        finalized_at = game.finalized_at
        next_start = _next_rostered_start(player_game, player_games, game_by_id)
        feasible = finalized_at is not None and next_start is not None and finalized_at < next_start
        checks.append(
            OracleFeasibilityCheck(
                sleeper_id=decision.player_id,
                game_id=decision.game_id,
                slot_index=decision.slot_index,
                kind="lockable_earlier",
                feasible=feasible,
                detail=(
                    f"finalized_at={None if finalized_at is None else finalized_at.isoformat()} "
                    f"next_rostered_start="
                    f"{None if next_start is None else next_start.isoformat()}"
                ),
            )
        )
    return tuple(checks)


def _is_final_rostered_game(
    player_game: ReplayPlayerGame,
    player_games: tuple[ReplayPlayerGame, ...],
    game_by_id: dict[str, ReplayGame],
) -> bool:
    """Return whether a player-game is the player's final rostered opportunity."""

    return _next_rostered_start(player_game, player_games, game_by_id) is None


def _next_rostered_start(
    player_game: ReplayPlayerGame,
    player_games: tuple[ReplayPlayerGame, ...],
    game_by_id: dict[str, ReplayGame],
) -> datetime | None:
    """Return the next rostered game start for the same player, if any."""

    current = game_by_id[player_game.game_id]
    later = tuple(
        game_by_id[other.game_id].start_time
        for other in player_games
        if other.sleeper_id == player_game.sleeper_id
        and other.rostered_at_tipoff
        and game_by_id[other.game_id].start_time > current.start_time
    )
    return min(later) if later else None


__all__ = ("oracle_feasibility_checks",)
