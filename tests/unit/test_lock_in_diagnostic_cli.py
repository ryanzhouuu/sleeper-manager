"""Coverage for historical Lock-In diagnostic command outcomes."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from lock_in_diagnostic_support import BASE, _team_week


def test_cli_writes_success_report_and_blocked_report(tmp_path: Path) -> None:
    from sleeper_manager.backtesting.artifacts import canonical_json_bytes
    from sleeper_manager.backtesting.experiments.lock_in_diagnostic import main
    from sleeper_manager.backtesting.replay.inputs import (
        ReplayInputManifest,
        SourceFingerprint,
        write_replay_input_bundle,
    )

    team_week = _team_week(p1_actual=30, p2_expected=1, p2_actual=1)
    manifest = ReplayInputManifest(
        league_id=team_week.league_id,
        season=team_week.season,
        builder_version="historical-team-week-bundle-v1",
        source_fingerprints=(SourceFingerprint("fixture", "abc123"),),
        scoring_policy_version="scoring-v1",
        scoring_policy_fingerprint="policy-fp",
        league_configuration_fingerprint="league-fp",
        roster_timeline_fingerprint="roster-fp",
        week_boundaries_fingerprint="week-fp",
        eligibility_policy_version="eligibility-v1",
        projection_config_version="projection-v1",
    )
    team_week = replace(team_week, manifest_id=manifest.manifest_id)
    bundle = write_replay_input_bundle(tmp_path / "inputs", manifest, (team_week,))
    team_week_path = bundle / "team-weeks/league-1/week-01/roster-1.json"
    output_root = tmp_path / "reports"

    code = main(
        [
            "--team-week-path",
            str(team_week_path),
            "--output-root",
            str(output_root),
            "--scenario-count",
            "32",
            "--seed",
            "9",
            "--planning-lead-time-seconds",
            "60",
        ]
    )
    assert code == 0
    reports = list(output_root.glob("lock-in-diagnostics/*/*/*.json"))
    assert len(reports) == 1
    first_bytes = reports[0].read_bytes()

    code_again = main(
        [
            "--team-week-path",
            str(team_week_path),
            "--output-root",
            str(output_root),
            "--scenario-count",
            "32",
            "--seed",
            "9",
            "--planning-lead-time-seconds",
            "60",
        ]
    )
    assert code_again == 0
    assert reports[0].read_bytes() == first_bytes

    blocked = _team_week(
        future_projection_available_as_of=BASE + timedelta(hours=10),
        include_second_p1_game=False,
    )
    blocked = replace(blocked, manifest_id=manifest.manifest_id)
    blocked_path = tmp_path / "blocked.json"
    blocked_path.write_bytes(canonical_json_bytes(blocked.to_dict()))
    blocked_code = main(
        [
            "--team-week-path",
            str(blocked_path),
            "--output-root",
            str(output_root / "blocked"),
            "--scenario-count",
            "16",
            "--seed",
            "1",
        ]
    )
    assert blocked_code == 1
    blocked_reports = list((output_root / "blocked").glob("lock-in-diagnostics/*/*/*.json"))
    assert len(blocked_reports) == 1
    assert b"blocked_no_evaluable_candidate" in blocked_reports[0].read_bytes()


def test_cli_returns_nonzero_for_invalid_artifact(tmp_path: Path) -> None:
    from sleeper_manager.backtesting.experiments.lock_in_diagnostic import main

    path = tmp_path / "broken.json"
    path.write_text("{")
    assert main(["--team-week-path", str(path), "--output-root", str(tmp_path / "out")]) == 2
