"""Generation and validation tests for cutoff-safe projection surfaces."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from lock_in_diagnostic_support import _projection_surface, _team_week
from team_week_projection_support import _nba_inputs

from sleeper_manager.backtesting.replay.projection_surface import (
    HistoricalProjectionSurfaceError,
    build_historical_projection_surface,
    validate_historical_projection_surface,
)
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.projections.direct_baseline import DirectFantasyPointBaseline


def test_surface_requires_every_future_player_game_at_each_cutoff() -> None:
    """Reject silently omitted projection attempts from an otherwise valid surface."""

    team_week = _team_week()
    surface = _projection_surface(team_week)
    incomplete = replace(surface, entries=surface.entries[1:])

    with pytest.raises(HistoricalProjectionSurfaceError, match="missing required entries"):
        validate_historical_projection_surface(incomplete, team_week=team_week)


def test_surface_rejects_team_week_identity_mismatch() -> None:
    """Prevent a valid projection surface from being paired with another bundle."""

    team_week = _team_week()
    surface = _projection_surface(team_week)

    with pytest.raises(HistoricalProjectionSurfaceError, match="team-week fingerprint"):
        validate_historical_projection_surface(
            surface,
            team_week=replace(team_week, roster_player_ids=("p2", "p1")),
        )


def test_surface_rejects_wrong_projection_configuration() -> None:
    """Require callers to select the projection version explicitly."""

    team_week = _team_week()

    with pytest.raises(HistoricalProjectionSurfaceError, match="configuration"):
        validate_historical_projection_surface(
            _projection_surface(team_week),
            team_week=team_week,
            projection_config_version="another-model",
        )


def test_builder_excludes_later_finalized_outcome_until_a_later_cutoff() -> None:
    """Change projections only after the intervening outcome becomes available."""

    first_game = datetime(2026, 2, 2, 20, tzinfo=UTC)
    second_game = datetime(2026, 2, 3, 20, tzinfo=UTC)
    original = _team_week(include_second_p1_game=False)
    team_week = replace(
        original,
        games=(
            replace(
                original.games[0], start_time=first_game, final_time=first_game + timedelta(hours=2)
            ),
            replace(
                original.games[1],
                start_time=second_game,
                final_time=second_game + timedelta(hours=2),
            ),
        ),
        player_games=tuple(
            replace(player_game, provider_player_id="provider-1")
            for player_game in original.player_games
        ),
    )
    scoring = ScoringPolicy(points=1)

    first = build_historical_projection_surface(
        team_week,
        _nba_inputs(same_day_points=1),
        DirectFantasyPointBaseline(),
        scoring_policy=scoring,
    )
    second = build_historical_projection_surface(
        team_week,
        _nba_inputs(same_day_points=100),
        DirectFantasyPointBaseline(),
        scoring_policy=scoring,
    )
    first_by_key = {entry.key: entry for entry in first.entries}
    second_by_key = {entry.key: entry for entry in second.entries}
    early_key = min(key for key in first_by_key if key[2] == "g2")
    late_key = max(key for key in first_by_key if key[2] == "g2")

    assert first_by_key[early_key].projection == second_by_key[early_key].projection
    assert first_by_key[late_key].projection != second_by_key[late_key].projection
