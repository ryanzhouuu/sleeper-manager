"""Regression coverage for selected historical team-week source evidence."""

from __future__ import annotations

from pathlib import Path

from team_week_source_support import (
    LEAGUE_ID,
    RETRIEVED_AT,
    _write_archive,
    _write_unrelated_week,
)

import sleeper_manager.backtesting.replay.team_week_sources as team_week_sources


def test_bundle_loads_only_the_requested_sleeper_week(tmp_path: Path) -> None:
    """Ignore unrelated cached weeks that are not fingerprinted by this bundle."""

    _write_archive(tmp_path)
    _write_unrelated_week(tmp_path)

    archive = team_week_sources.load_selected_archive(
        tmp_path,
        league_id=LEAGUE_ID,
        roster_id=4,
        week=16,
        fallback_retrieved_at=RETRIEVED_AT,
    )

    assert tuple(matchup.week for matchup in archive.matchup_weeks) == (16,)
    assert not archive.transactions


def test_nba_season_comes_from_sleeper_archive_metadata(tmp_path: Path) -> None:
    """Use Sleeper's season rather than a Monday's calendar year at season boundaries."""

    _write_archive(tmp_path, season="2025")
    archive = team_week_sources.load_selected_archive(
        tmp_path,
        league_id=LEAGUE_ID,
        roster_id=4,
        week=16,
        fallback_retrieved_at=RETRIEVED_AT,
    )

    assert team_week_sources.nba_season_from_archive(archive) == 2026
