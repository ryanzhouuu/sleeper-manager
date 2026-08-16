import json
from pathlib import Path

from sleeper_manager.backtesting.lock_in_experiment import (
    LockInExperimentError,
    _load_cached_archive,
    run_lock_in_policy_validation,
)


def test_lock_in_experiment_fails_closed_without_cached_archives(tmp_path: Path) -> None:
    try:
        run_lock_in_policy_validation(
            tmp_path,
            current_league_id="current",
            historical_league_id="historical",
            stress_league_id="stress",
        )
    except LockInExperimentError as error:
        assert "Missing cached Sleeper artifact" in str(error)
    else:
        raise AssertionError("missing cached archives must fail closed")


def test_cached_archive_uses_requested_league_when_it_has_events(tmp_path: Path) -> None:
    _write_archive(tmp_path, "requested", season="2026", previous=None, has_events=True)

    archive = _load_cached_archive(tmp_path, "requested")

    assert archive.league_id == "requested"
    assert archive.season == "2026"
    assert archive.resolved_from_league_id is None


def test_cached_archive_uses_immediate_eventful_predecessor(tmp_path: Path) -> None:
    _write_archive(tmp_path, "shell", season="2026", previous="prior", has_events=False)
    _write_archive(tmp_path, "prior", season="2025", previous=None, has_events=True)

    archive = _load_cached_archive(tmp_path, "shell")

    assert archive.league_id == "prior"
    assert archive.season == "2025"
    assert archive.resolved_from_league_id == "shell"


def test_cached_archive_uses_nearest_eventful_predecessor_in_three_generation_chain(
    tmp_path: Path,
) -> None:
    _write_archive(tmp_path, "shell", season="2026", previous="prior", has_events=False)
    _write_archive(tmp_path, "prior", season="2025", previous="oldest", has_events=True)
    _write_archive(tmp_path, "oldest", season="2024", previous=None, has_events=True)

    archive = _load_cached_archive(tmp_path, "shell")

    assert archive.league_id == "prior"
    assert archive.season == "2025"
    assert archive.resolved_from_league_id == "shell"


def test_cached_archive_falls_back_to_requested_league_without_events(tmp_path: Path) -> None:
    _write_archive(tmp_path, "shell", season="2026", previous="prior", has_events=False)
    _write_archive(tmp_path, "prior", season="2025", previous=None, has_events=False)

    archive = _load_cached_archive(tmp_path, "shell")

    assert archive.league_id == "shell"
    assert archive.season == "2026"
    assert archive.resolved_from_league_id is None


def _write_archive(
    workspace: Path,
    league_id: str,
    *,
    season: str,
    previous: str | None,
    has_events: bool,
) -> None:
    root = workspace / "sleeper" / league_id
    root.mkdir(parents=True)
    league = {
        "league_id": league_id,
        "previous_league_id": previous,
        "sport": "nba",
        "season": season,
        "season_type": "regular",
        "status": "complete",
        "total_rosters": 1,
        "roster_positions": ["PG", "BN"],
        "scoring_settings": {"pts": 1},
    }
    (root / "league.json").write_text(json.dumps(league), encoding="utf-8")
    (root / "rosters.json").write_text("[]", encoding="utf-8")
    if has_events:
        week = root / "weeks" / "1"
        week.mkdir(parents=True)
        (week / "matchups.json").write_text(
            json.dumps([{"roster_id": 1, "players": [], "starters": []}]),
            encoding="utf-8",
        )
