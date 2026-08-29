"""Regression coverage for point-in-time historical team-week projections."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from team_week_projection_support import (
    _nba_inputs,
)
from team_week_source_support import (
    LEAGUE_ID,
    RETRIEVED_AT,
    _write_archive,
)

import sleeper_manager.backtesting.replay.team_week_projection as team_week_projection
import sleeper_manager.backtesting.replay.team_week_sources as team_week_sources
from sleeper_manager.backtesting.replay.roster_timeline import build_fantasy_week_boundaries
from sleeper_manager.projections.direct_baseline import DirectFantasyPointBaseline


def test_approximate_finalization_waits_until_six_eastern_after_late_game() -> None:
    """Avoid declaring an 11 PM Eastern game finalized during its first hour."""

    late_tipoff = datetime(2026, 2, 3, 4, tzinfo=UTC)

    assert team_week_projection.approximate_finalization_at(late_tipoff) == datetime(
        2026, 2, 3, 11, tzinfo=UTC
    )


def test_player_mappings_preserve_resolved_and_missing_catalog_identities(tmp_path: Path) -> None:
    """Keep unresolved roster identities visible instead of silently dropping them."""

    _write_archive(tmp_path)
    mappings = team_week_projection.player_mappings(
        ("sleeper-1", "missing-player"),
        team_week_sources.player_catalog(tmp_path, LEAGUE_ID),
        _nba_inputs(),
    )

    assert [(item.sleeper_id, item.espn_id) for item in mappings] == [
        ("missing-player", None),
        ("sleeper-1", "provider-1"),
    ]


def test_projection_snapshots_exclude_same_day_outcomes(tmp_path: Path) -> None:
    """Build a target projection from evidence finalized before its decision time."""

    _write_archive(tmp_path)
    archive = team_week_sources.load_selected_archive(
        tmp_path,
        league_id=LEAGUE_ID,
        roster_id=4,
        week=16,
        fallback_retrieved_at=RETRIEVED_AT,
    )
    boundary = build_fantasy_week_boundaries({16: date(2026, 2, 2)})[0]
    catalog = team_week_sources.player_catalog(tmp_path, LEAGUE_ID)
    baseline = DirectFantasyPointBaseline()
    first_inputs = _nba_inputs(same_day_points=1)
    second_inputs = _nba_inputs(same_day_points=100)
    first_mappings = team_week_projection.player_mappings(("sleeper-1",), catalog, first_inputs)
    second_mappings = team_week_projection.player_mappings(("sleeper-1",), catalog, second_inputs)

    first = team_week_projection.projection_snapshots(
        first_inputs, first_mappings, boundary, archive, baseline
    )
    second = team_week_projection.projection_snapshots(
        second_inputs, second_mappings, boundary, archive, baseline
    )

    first_target = next(item for item in first if item.game_id == "target")
    second_target = next(item for item in second if item.game_id == "target")
    assert first_target.distribution == second_target.distribution
    assert first_target.input_version == second_target.input_version


def test_selected_games_include_only_mapped_player_evidence(tmp_path: Path) -> None:
    """Exclude unrelated box scores and attach the approximate finalization bound."""

    _write_archive(tmp_path)
    inputs = _nba_inputs()
    mappings = team_week_projection.player_mappings(
        ("sleeper-1",),
        team_week_sources.player_catalog(tmp_path, LEAGUE_ID),
        inputs,
    )
    boundary = build_fantasy_week_boundaries({16: date(2026, 2, 2)})[0]

    games, box_scores = team_week_projection.selected_games_and_box_scores(
        inputs, mappings, boundary
    )

    assert tuple(game.provider_id for game in games) == ("same-day", "target")
    assert {(item.player_id, item.game_id) for item in box_scores} == {
        ("provider-1", "same-day"),
        ("provider-1", "target"),
    }
    assert all(game.finalized_at is not None for game in games)
