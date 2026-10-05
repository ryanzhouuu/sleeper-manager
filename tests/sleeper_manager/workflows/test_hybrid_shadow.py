"""Executable paired shadow output, strict evidence input, and control compatibility."""

import json
from dataclasses import replace

import pytest

from sleeper_manager.backtesting.artifacts import atomic_write_json, canonical_json
from sleeper_manager.backtesting.replay.hybrid_projection_surface import (
    build_hybrid_projection_surfaces,
)
from sleeper_manager.backtesting.replay.projection_surface_artifact import (
    load_historical_projection_surface_artifact,
    write_historical_projection_surface_artifact,
)
from sleeper_manager.projections.hybrid_config import HybridProjectionError
from sleeper_manager.projections.hybrid_history_artifact import (
    CutoffAvailability,
    load_hybrid_availability_artifact,
    load_hybrid_history_artifact,
)
from sleeper_manager.projections.hybrid_provider import HybridProjectionProvider
from sleeper_manager.workflows.hybrid_shadow import compare_hybrid_shadow
from sleeper_manager.workflows.hybrid_shadow_cli import main
from tests.sleeper_manager.backtesting.experiments.lock_in_diagnostic_support import (
    _projection_surface,
)
from tests.sleeper_manager.backtesting.replay.test_hybrid_projection_surface import opening_week
from tests.sleeper_manager.projections.hybrid_support import history
from tests.sleeper_manager.projections.test_hybrid_model import POLICY


def controls(weeks):  # type: ignore[no-untyped-def]
    return tuple(
        replace(
            _projection_surface(week),
            scoring_policy_version=POLICY.version,
            entries=tuple(
                replace(
                    entry,
                    projection=replace(entry.projection, scoring_policy_version=POLICY.version),
                )
                for entry in _projection_surface(week).entries
            ),
        )
        for week in weeks
    )


def test_shadow_pairs_both_rosters_and_preserves_failed_candidates() -> None:
    weeks = (opening_week(1), opening_week(2))
    built = build_hybrid_projection_surfaces(
        weeks, HybridProjectionProvider(history()), scoring_policy=POLICY
    )
    compared = compare_hybrid_shadow(weeks, built, controls(weeks))
    assert len(compared.pairs) == 12
    assert {pair.roster_id for pair in compared.pairs} == {1, 2}
    assert all(pair.expected_score_delta is not None for pair in compared.pairs)
    assert compared.fingerprint == compare_hybrid_shadow(weeks, built, controls(weeks)).fingerprint
    blocked = build_hybrid_projection_surfaces(
        weeks, HybridProjectionProvider(replace(history(), opportunities=())), scoring_policy=POLICY
    )
    assert all(
        pair.expected_score_delta is None and pair.candidate.failure
        for pair in compare_hybrid_shadow(weeks, blocked, controls(weeks)).pairs
    )


def test_shadow_rejects_wrong_scoring_and_incomplete_attempt_evidence() -> None:
    weeks = (opening_week(),)
    built = build_hybrid_projection_surfaces(
        weeks, HybridProjectionProvider(history()), scoring_policy=POLICY
    )
    with pytest.raises(HybridProjectionError, match="scoring_or_cutoff"):
        compare_hybrid_shadow(weeks, built, (_projection_surface(weeks[0]),))
    with pytest.raises(HybridProjectionError, match="attempt_evidence"):
        compare_hybrid_shadow(weeks, replace(built, attempts=()), controls(weeks))


def test_cli_writes_valid_two_roster_sidecars_and_full_diagnostics(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    weeks = (opening_week(1), opening_week(2))
    week_paths = [tmp_path / f"team-{week.roster_id}.json" for week in weeks]
    control_paths = [tmp_path / f"control-{week.roster_id}.json" for week in weeks]
    for week, control, week_path, control_path in zip(
        weeks, controls(weeks), week_paths, control_paths, strict=True
    ):
        atomic_write_json(week_path, week.to_dict())
        write_historical_projection_surface_artifact(control_path, control)
    history_path = tmp_path / "history.json"
    atomic_write_json(history_path, history())
    league_path = tmp_path / "league.json"
    atomic_write_json(
        league_path,
        {
            "scoring_settings": {
                "pts": 1,
                "reb": 1.2,
                "ast": 1.5,
                "dd": 8,
                "td": 15,
                "tf": -4,
                "ff": -6,
            }
        },
    )
    output = tmp_path / "output"
    argv = [
        "--history",
        str(history_path),
        "--league",
        str(league_path),
        "--team-week",
        *map(str, week_paths),
        "--control-surface",
        *map(str, control_paths),
        "--output-dir",
        str(output),
    ]
    assert main(argv) == 0
    first = (output / "shadow.json").read_bytes()
    assert "12 paired opportunities, 0 blocked" in capsys.readouterr().out
    for week in weeks:
        surface = load_historical_projection_surface_artifact(
            output / f"roster-{week.roster_id}-candidate.json", team_week=week
        )
        assert len(surface.entries) == 6
    payload = json.loads(first)
    assert len(payload["comparison"]["pairs"]) == 12
    assert payload["comparison"]["pairs"][0]["candidate"]["joint_weights"]
    assert main(argv) == 0
    assert (output / "shadow.json").read_bytes() == first


@pytest.mark.parametrize(
    "corruption", ["extra", "coerce", "naive", "uncovered", "duplicate", "nested"]
)
def test_history_artifact_rejects_ambiguous_or_unverified_input(tmp_path, corruption: str) -> None:  # type: ignore[no-untyped-def]
    raw = json.loads(canonical_json(history()))
    row = raw["observations"][0]
    if corruption == "extra":
        raw["typo"] = True
    if corruption == "coerce":
        row["did_play"] = "true"
    if corruption == "naive":
        row["game_start"] = "2026-03-01T00:00:00"
    if corruption == "uncovered":
        del row["line"]["points"]
    if corruption == "nested":
        row["line"]["unknown"] = 1
    encoded = json.dumps(raw)
    if corruption == "duplicate":
        encoded = encoded.replace(
            '"dataset_version": "dataset-v1"',
            '"dataset_version": "first", "dataset_version": "dataset-v1"',
        )
    path = tmp_path / "history.json"
    path.write_text(encoded)
    with pytest.raises(HybridProjectionError):
        load_hybrid_history_artifact(path)


def test_history_artifact_round_trips_explicit_evidence(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "history.json"
    atomic_write_json(path, history())
    assert load_hybrid_history_artifact(path) == history()


def test_cutoff_availability_artifact_preserves_report_groups_and_rejects_duplicates(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    from sleeper_manager.domain.nba import AvailabilityStatus
    from sleeper_manager.integrations.nba.historical_feature_models import AvailabilityObservation
    from sleeper_manager.projections.hybrid_types import GameAvailability
    from tests.sleeper_manager.projections.hybrid_support import CUTOFF

    report = GameAvailability(
        "next", CUTOFF, AvailabilityStatus.UNKNOWN, AvailabilityObservation.MISSING_REPORT, "v1"
    )
    assignment = CutoffAvailability(CUTOFF, "sleeper-player", report)
    path = tmp_path / "availability.json"
    atomic_write_json(path, (assignment,))
    assert load_hybrid_availability_artifact(path) == {(CUTOFF, "sleeper-player", "next"): report}
    atomic_write_json(path, (assignment, assignment))
    with pytest.raises(HybridProjectionError, match="duplicate_availability"):
        load_hybrid_availability_artifact(path)
