"""Shared deterministic fixtures for historical Lock-In diagnostic tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sleeper_manager.backtesting.replay.inputs.models import (
    HistoricalTeamWeekInput,
    ReplayCoverageSummary,
)
from sleeper_manager.backtesting.replay.models import ReplayGame, ReplayGameStatus, ReplayPlayerGame
from sleeper_manager.backtesting.replay.projection_surface import (
    HistoricalProjectionSurface,
    ProjectionSurfaceEntry,
    full_advisor_planning_cutoffs,
    team_week_fingerprint,
)
from sleeper_manager.domain.planning import PlanningQuality
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot

BASE = datetime(2026, 2, 2, 18, tzinfo=UTC)


def _team_week(
    *,
    eligibility: PlanningQuality = PlanningQuality.BEST_KNOWN_CONSTRAINTS_ORACLE,
    exact: int = 0,
    best_known: int = 2,
    p1_actual: float = 25,
    p2_expected: float = 8,
    p2_actual: float = 8,
    future_projection_available_as_of: datetime | None = None,
    include_second_p1_game: bool = True,
) -> HistoricalTeamWeekInput:
    """Build a complete two-starter diagnostic fixture with configurable evidence."""

    g1_start = BASE
    g1_final = BASE + timedelta(hours=2)
    g2_start = BASE + timedelta(hours=6)
    g2_final = BASE + timedelta(hours=8)
    g3_start = BASE + timedelta(hours=12)
    g3_final = BASE + timedelta(hours=14)
    future_as_of = future_projection_available_as_of or (BASE - timedelta(hours=1))
    games = [
        ReplayGame("g1", g1_start, g1_final, 1, ("home", "away"), ReplayGameStatus.FINAL),
        ReplayGame("g2", g2_start, g2_final, 1, ("home", "away"), ReplayGameStatus.FINAL),
    ]
    player_games = [
        _player_game(
            "p1",
            "g1",
            p1_actual,
            expected=p1_actual,
            available_as_of=BASE - timedelta(hours=1),
        ),
        _player_game(
            "p2",
            "g2",
            p2_actual,
            expected=p2_expected,
            available_as_of=future_as_of,
        ),
    ]
    if include_second_p1_game:
        games.append(
            ReplayGame("g3", g3_start, g3_final, 1, ("home", "away"), ReplayGameStatus.FINAL)
        )
        player_games.append(
            _player_game(
                "p1",
                "g3",
                4,
                expected=4,
                available_as_of=BASE - timedelta(hours=1),
            )
        )
    count = len(player_games)
    return HistoricalTeamWeekInput(
        manifest_id="manifest-diagnostic",
        league_id="league-1",
        season="2026",
        week=1,
        roster_id=1,
        starter_slots=("UTIL", "UTIL"),
        roster_player_ids=("p1", "p2"),
        observed_starter_ids=("p1", "p2"),
        games=tuple(games),
        player_games=tuple(player_games),
        eligibility_quality=eligibility,
        coverage=ReplayCoverageSummary(
            expected_player_games=count,
            joined_player_games=count,
            resolved_identities=count,
            exact_eligibility=(
                exact if exact else (count if eligibility is PlanningQuality.EXACT else 0)
            ),
            best_known_eligibility=(
                best_known if eligibility is PlanningQuality.BEST_KNOWN_CONSTRAINTS_ORACLE else 0
            ),
            scored_player_games=count,
            projected_player_games=count,
        ),
    )


def _player_game(
    player_id: str,
    game_id: str,
    actual: float,
    *,
    expected: float,
    available_as_of: datetime,
) -> ReplayPlayerGame:
    """Build one rostered player-game with a deterministic projection."""

    return ReplayPlayerGame(
        sleeper_id=player_id,
        provider_player_id=player_id,
        game_id=game_id,
        fantasy_team_id=1,
        rostered_at_tipoff=True,
        eligible_positions=("PG",),
        actual_score=actual,
        projection=ProjectionSnapshot(
            player_id=player_id,
            game_id=game_id,
            available_as_of=available_as_of,
            model_version="fixture-model",
            input_version=f"inputs-{player_id}-{game_id}",
            scoring_policy_version="scoring-v1",
            distribution=ProjectionDistribution.from_weighted_observations(((expected, 1.0),)),
            reasons=(),
        ),
    )


def _projection_surface(
    team_week: HistoricalTeamWeekInput,
    *,
    fail_key: tuple[datetime, str, str] | None = None,
) -> HistoricalProjectionSurface:
    """Build a complete cutoff surface from fixture projection distributions."""

    games = {game.game_id: game for game in team_week.games}
    entries = []
    for cutoff in full_advisor_planning_cutoffs(team_week):
        for player_game in team_week.player_games:
            if games[player_game.game_id].start_time <= cutoff:
                continue
            key = (cutoff, player_game.sleeper_id, player_game.game_id)
            if key == fail_key:
                entries.append(
                    ProjectionSurfaceEntry(
                        cutoff,
                        player_game.sleeper_id,
                        player_game.game_id,
                        failure_reason="missing_warmup_history",
                        failure_detail="fixture failure",
                    )
                )
                continue
            assert player_game.projection is not None
            entries.append(
                ProjectionSurfaceEntry(
                    cutoff,
                    player_game.sleeper_id,
                    player_game.game_id,
                    projection=replace(player_game.projection, available_as_of=cutoff),
                )
            )
    return HistoricalProjectionSurface(
        team_week_manifest_id=team_week.manifest_id,
        league_id=team_week.league_id,
        season=team_week.season,
        week=team_week.week,
        roster_id=team_week.roster_id,
        team_week_fingerprint=team_week_fingerprint(team_week),
        projection_config_version="fixture-model",
        scoring_policy_version="scoring-v1",
        source_fingerprints=(),
        entries=tuple(entries),
    )
