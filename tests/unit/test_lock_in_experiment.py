import json
from pathlib import Path

from sleeper_manager.backtesting.experiments.lock_in import (
    LockInExperimentError,
    LockInValidationOutput,
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


def test_lock_in_status_reports_missing_replay_inputs(tmp_path: Path) -> None:
    _write_validation_archives(tmp_path)

    output = _run_validation(tmp_path)
    report = json.loads(output.report_json_path.read_text(encoding="utf-8"))

    assert output.status == "blocked_missing_replay_inputs"
    assert report["status"] == output.status
    assert report["replay_inputs"]["leagues"]["historical"]["team_week_count"] == 0


def test_lock_in_status_advances_when_all_replay_inputs_are_complete(tmp_path: Path) -> None:
    _write_validation_archives(tmp_path)
    for league_id in ("current", "historical", "stress"):
        _write_replay_team_week(tmp_path, league_id, complete=True)

    output = _run_validation(tmp_path)
    report = json.loads(output.report_json_path.read_text(encoding="utf-8"))

    assert output.status == "ready_for_replay_validation"
    assert report["replay_inputs"]["selected_manifest_id"] == "manifest-1"


def test_lock_in_status_reports_incomplete_replay_inputs(tmp_path: Path) -> None:
    _write_validation_archives(tmp_path)
    _write_replay_team_week(tmp_path, "current", complete=True)
    _write_replay_team_week(tmp_path, "historical", complete=False)
    _write_replay_team_week(tmp_path, "stress", complete=True)

    output = _run_validation(tmp_path)

    assert output.status == "blocked_incomplete_replay_inputs"


def test_lock_in_status_does_not_combine_unrelated_manifests(tmp_path: Path) -> None:
    _write_validation_archives(tmp_path)
    _write_replay_team_week(tmp_path, "current", complete=True, manifest_id="manifest-a")
    _write_replay_team_week(tmp_path, "historical", complete=True, manifest_id="manifest-b")
    _write_replay_team_week(tmp_path, "stress", complete=True, manifest_id="manifest-c")

    output = _run_validation(tmp_path)

    assert output.status == "blocked_incoherent_replay_inputs"


def _run_validation(tmp_path: Path) -> LockInValidationOutput:
    return run_lock_in_policy_validation(
        tmp_path,
        current_league_id="current",
        historical_league_id="historical",
        stress_league_id="stress",
    )


def _write_validation_archives(tmp_path: Path) -> None:
    for league_id in ("current", "historical", "stress"):
        _write_archive(tmp_path, league_id, season="2026", previous=None, has_events=True)


def _write_replay_team_week(
    tmp_path: Path,
    league_id: str,
    *,
    complete: bool,
    manifest_id: str = "manifest-1",
) -> None:
    path = (
        tmp_path
        / "team-week-inputs"
        / manifest_id
        / "team-weeks"
        / league_id
        / "week-01"
        / "roster-1.json"
    )
    path.parent.mkdir(parents=True)
    manifest_path = tmp_path / "team-week-inputs" / manifest_id / "manifest.json"
    manifest_path.write_text(json.dumps({"manifest_id": manifest_id}), encoding="utf-8")
    path.write_text(json.dumps({"league_id": league_id, "complete": complete}), encoding="utf-8")


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
    week = root / "weeks" / "1"
    week.mkdir(parents=True)
    matchups = [{"roster_id": 1, "players": [], "starters": []}] if has_events else []
    (week / "matchups.json").write_text(json.dumps(matchups), encoding="utf-8")
