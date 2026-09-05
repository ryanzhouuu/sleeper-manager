"""Regression coverage for historical team-week bundle orchestration."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from team_week_projection_support import (
    _nba_inputs,
)
from team_week_source_support import (
    LEAGUE_ID,
    RETRIEVED_AT,
    _write_archive,
)

from sleeper_manager.backtesting.replay.team_week_bundle import (
    HistoricalTeamWeekBundleError,
    HistoricalTeamWeekBundleRequest,
    bootstrap_historical_team_week_bundle,
)
from sleeper_manager.domain.planning import PlanningQuality


def test_bootstrap_writes_one_labeled_team_week_with_pregame_projection(tmp_path: Path) -> None:
    """Persist the selected team-week with best-known eligibility and one safe projection."""

    _write_archive(tmp_path)

    output = bootstrap_historical_team_week_bundle(
        tmp_path,
        HistoricalTeamWeekBundleRequest(
            LEAGUE_ID,
            roster_id=4,
            week=16,
            monday=date(2026, 2, 2),
        ),
        nba_inputs=_nba_inputs(),
        now=RETRIEVED_AT,
    )

    team_week = output.team_week
    assert team_week.league_id == LEAGUE_ID
    assert team_week.week == 16
    assert team_week.roster_id == 4
    assert team_week.eligibility_quality is PlanningQuality.BEST_KNOWN_CONSTRAINTS_ORACLE
    assert team_week.coverage.best_known_eligibility == 2
    assert team_week.coverage.projected_player_games == 2
    assert not team_week.exclusions
    projection = next(
        item.projection for item in team_week.player_games if item.game_id == "target"
    )
    assert projection is not None
    assert projection.available_as_of == datetime(2026, 2, 2, 19, 30, tzinfo=UTC)
    assert output.bundle_root.is_dir()
    assert output.manifest_path.is_file()
    assert output.team_week_path.is_file()
    finalization_bound = next(
        fingerprint
        for fingerprint in output.manifest.source_fingerprints
        if fingerprint.name == "approximate-finalization-bound"
    )
    assert finalization_bound.version == "approximate-next-eastern-day-0600-v1"
    assert (
        output.manifest.eligibility_policy_version
        == "observed-weekly-starters-current-catalog-best-known-v2"
    )


def test_bootstrap_excludes_same_day_outcome_from_pregame_projection(tmp_path: Path) -> None:
    """Keep results from the target's local game day out of its projection history."""

    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    _write_archive(first_workspace)
    _write_archive(second_workspace)
    request = HistoricalTeamWeekBundleRequest(
        LEAGUE_ID,
        roster_id=4,
        week=16,
        monday=date(2026, 2, 2),
    )

    baseline = bootstrap_historical_team_week_bundle(
        first_workspace,
        request,
        nba_inputs=_nba_inputs(same_day_points=1),
        now=RETRIEVED_AT,
    )
    changed = bootstrap_historical_team_week_bundle(
        second_workspace,
        request,
        nba_inputs=_nba_inputs(same_day_points=100),
        now=RETRIEVED_AT,
    )

    first = next(
        item.projection for item in baseline.team_week.player_games if item.game_id == "target"
    )
    second = next(
        item.projection for item in changed.team_week.player_games if item.game_id == "target"
    )
    assert first is not None
    assert second is not None
    assert first.distribution == second.distribution
    assert first.input_version == second.input_version


def test_bundle_marks_only_observed_starters_as_best_known_lock_eligible(tmp_path: Path) -> None:
    """Keep roster membership visible without granting unobserved bench lock permission."""

    _write_archive(tmp_path, include_bench=True)

    output = bootstrap_historical_team_week_bundle(
        tmp_path,
        HistoricalTeamWeekBundleRequest(
            LEAGUE_ID,
            roster_id=4,
            week=16,
            monday=date(2026, 2, 2),
        ),
        nba_inputs=_nba_inputs(include_bench=True),
        now=RETRIEVED_AT,
    )

    lock_eligibility = {
        player_game.sleeper_id: player_game.rostered_at_tipoff
        for player_game in output.team_week.player_games
        if player_game.game_id == "target"
    }
    assert lock_eligibility == {"sleeper-1": True, "sleeper-2": False}
    assert output.team_week.roster_player_ids == ("sleeper-1", "sleeper-2")


def test_bootstrap_detects_a_game_missing_from_the_raw_player_outcomes(tmp_path: Path) -> None:
    """A schedule-only game between agreeing team records must block complete assembly."""
    _write_archive(tmp_path)
    inputs = _nba_inputs()
    missing = replace(
        inputs.games[-1], provider_id="missing", start_time=datetime(2026, 2, 2, 18, tzinfo=UTC)
    )
    request = HistoricalTeamWeekBundleRequest(LEAGUE_ID, 4, 16, date(2026, 2, 2))
    with pytest.raises(HistoricalTeamWeekBundleError, match="no joined player-game evidence"):
        bootstrap_historical_team_week_bundle(
            tmp_path,
            request,
            nba_inputs=replace(inputs, games=(*inputs.games, missing)),
            now=RETRIEVED_AT,
        )

    filled = replace(inputs.player_box_scores[1], game_id="missing", played_at=missing.start_time)
    result = bootstrap_historical_team_week_bundle(
        tmp_path,
        request,
        nba_inputs=replace(
            inputs,
            games=(*inputs.games, missing),
            player_box_scores=(*inputs.player_box_scores, filled),
        ),
        now=RETRIEVED_AT,
    )
    assert result.team_week.coverage.expected_player_games == 3
    assert result.team_week.coverage.scored_player_games == 3
