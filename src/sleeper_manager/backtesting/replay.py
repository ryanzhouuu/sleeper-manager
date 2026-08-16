from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sleeper_manager.backtesting.replay_models import (
    LockedSlot,
    ReplayDecision,
    ReplayGame,
    ReplayPlayerGame,
    TeamWeekComparison,
    TeamWeekReplayResult,
)
from sleeper_manager.backtesting.replay_state import ReplayError, ReplayState
from sleeper_manager.decisions.lineup import (
    AssignmentCandidate,
    AssignmentResult,
    maximum_weight_assignment,
)


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    starter_slots: tuple[str, ...]
    league_id: str = "fixture"
    week: int = 1
    roster_id: int = 1
    eligibility_quality: str = "best_known_constraints_oracle"


def optimize_oracle(
    player_games: Iterable[ReplayPlayerGame],
    *,
    starter_slots: tuple[str, ...],
    team_id: int | None = None,
) -> AssignmentResult:
    candidates = tuple(
        AssignmentCandidate(
            candidate_id=(
                f"{game.sleeper_id}:{game.game_id}:"
                f"{game.membership_segment or game.fantasy_team_id}"
            ),
            player_id=game.sleeper_id,
            score=game.actual_score,
            eligible_positions=game.eligible_positions,
            game_id=game.game_id,
        )
        for game in player_games
        if game.rostered_at_tipoff and (team_id is None or game.fantasy_team_id == team_id)
    )
    return maximum_weight_assignment(candidates, starter_slots)


def oracle_team_week_result(
    player_games: Iterable[ReplayPlayerGame],
    *,
    config: ReplayConfig,
    games: Iterable[ReplayGame] = (),
) -> TeamWeekReplayResult:
    player_game_records = tuple(player_games)
    assignment = optimize_oracle(
        player_game_records,
        starter_slots=config.starter_slots,
        team_id=config.roster_id,
    )
    game_by_id = {game.game_id: game for game in games}
    locked: list[LockedSlot] = []
    automatic: list[tuple[str, float]] = []
    decisions: list[ReplayDecision] = []
    selected_players = {
        item.player_id for item in assignment.assignments if item.player_id is not None
    }
    for item in assignment.assignments:
        if item.player_id is None or item.game_id is None:
            continue
        replay_game = game_by_id.get(item.game_id)
        locked_at = (
            replay_game.final_time
            if replay_game and replay_game.final_time
            else datetime.min.replace(tzinfo=UTC)
        )
        player_game = next(
            game
            for game in player_game_records
            if game.sleeper_id == item.player_id and game.game_id == item.game_id
        )
        final_game = _is_final_player_game(player_game, player_game_records, game_by_id)
        if final_game:
            automatic.append((item.player_id, item.score))
        else:
            locked.append(
                LockedSlot(
                    item.slot_index,
                    item.slot_position,
                    item.player_id,
                    item.game_id,
                    item.score,
                    locked_at,
                )
            )
        decisions.append(
            ReplayDecision(
                decision_time=locked_at,
                kind="oracle_select",
                player_id=item.player_id,
                game_id=item.game_id,
                slot_index=item.slot_index,
                information_version="realized-outcomes",
                expected_terminal_score=item.score,
                counterfactual_value=item.score,
                reason="Constrained maximum-weight realized assignment.",
            )
        )
    automatic.extend(
        (player_id, _automatic_final_score(player_id, player_game_records, game_by_id))
        for player_id in sorted({game.sleeper_id for game in player_game_records})
        if player_id not in selected_players
    )
    return TeamWeekReplayResult(
        league_id=config.league_id,
        week=config.week,
        roster_id=config.roster_id,
        policy_name="oracle",
        realized_score=round(assignment.score, 6),
        decisions=tuple(decisions),
        locked_slots=tuple(locked),
        automatic_final_scores=tuple(automatic),
        eligibility_quality=config.eligibility_quality,
        data_quality="complete" if not config.eligibility_quality == "unknown" else "partial",
    )


def compare_team_week(
    oracle: TeamWeekReplayResult,
    model_policy: TeamWeekReplayResult,
) -> TeamWeekComparison:
    if (oracle.league_id, oracle.week, oracle.roster_id) != (
        model_policy.league_id,
        model_policy.week,
        model_policy.roster_id,
    ):
        raise ReplayError("Oracle and model policy do not share the same team-week")
    regret = round(oracle.realized_score - model_policy.realized_score, 6)
    if regret < -1e-6:
        raise ReplayError("Model policy exceeded the oracle; replay state is inconsistent")
    capture = (
        round(model_policy.realized_score / oracle.realized_score, 6)
        if oracle.realized_score > 0
        else None
    )
    return TeamWeekComparison(
        oracle_team_score=oracle.realized_score,
        model_policy_team_score=model_policy.realized_score,
        lock_in_regret=max(regret, 0.0),
        score_capture=capture,
        invariant_results=(
            ("shared_team_week", True),
            ("nonnegative_regret", regret >= -1e-6),
            ("model_not_above_oracle", regret >= -1e-6),
        ),
    )


def _is_final_player_game(
    player_game: ReplayPlayerGame,
    player_games: tuple[ReplayPlayerGame, ...],
    game_by_id: dict[str, ReplayGame],
) -> bool:
    current_start = _game_start(player_game.game_id, game_by_id, datetime.max.replace(tzinfo=UTC))
    later = tuple(
        other
        for other in player_games
        if other.sleeper_id == player_game.sleeper_id
        and other.fantasy_team_id == player_game.fantasy_team_id
        and _game_start(other.game_id, game_by_id, datetime.max.replace(tzinfo=UTC)) > current_start
    )
    return not later


def _automatic_final_score(
    player_id: str,
    player_games: tuple[ReplayPlayerGame, ...],
    game_by_id: dict[str, ReplayGame],
) -> float:
    candidates = tuple(game for game in player_games if game.sleeper_id == player_id)
    if not candidates:
        return 0.0
    final = max(
        candidates,
        key=lambda game: (
            _game_start(game.game_id, game_by_id, datetime.min.replace(tzinfo=UTC)),
            game.game_id,
        ),
    )
    return final.actual_score if final.rostered_at_tipoff else 0.0


def _game_start(game_id: str, game_by_id: dict[str, ReplayGame], default: datetime) -> datetime:
    game = game_by_id.get(game_id)
    return game.start_time if game is not None else default


__all__ = (
    "ReplayConfig",
    "ReplayError",
    "ReplayState",
    "compare_team_week",
    "optimize_oracle",
    "oracle_team_week_result",
)
