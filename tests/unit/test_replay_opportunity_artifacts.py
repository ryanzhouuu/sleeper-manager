"""Persisted membership uncertainty must not disappear during artifact decoding."""

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from historical_team_week_artifact_support import _team_week, _write_lone_payload

from sleeper_manager.backtesting.replay.inputs import (
    HistoricalTeamWeekArtifactError,
    PlayerTeamObservation,
    ReplayInputError,
    load_historical_team_week_artifact,
)


def test_inferred_membership_round_trips_without_becoming_complete(tmp_path: Path) -> None:
    original = _team_week("manifest-1")
    coverage = replace(
        original.coverage, exact_eligibility=1, best_known_eligibility=0, missing_evidence=()
    )
    assert coverage.complete
    original = replace(
        original, coverage=replace(coverage, inferred_team_membership=1), exclusions=()
    )
    path = _write_lone_payload(tmp_path, original)
    loaded = load_historical_team_week_artifact(path)

    assert loaded.coverage.inferred_team_membership == 1
    assert not loaded.complete
    assert loaded == original


def test_legacy_artifact_without_membership_count_remains_readable(tmp_path: Path) -> None:
    path = _write_lone_payload(tmp_path, _team_week("manifest-1"))
    payload = json.loads(path.read_text())
    del payload["coverage"]["inferred_team_membership"]
    path.write_text(json.dumps(payload))

    assert load_historical_team_week_artifact(path).coverage.inferred_team_membership == 0


@pytest.mark.parametrize("count", [True, -1, "1", 999])
def test_invalid_inferred_membership_count_is_rejected(tmp_path: Path, count: object) -> None:
    path = _write_lone_payload(tmp_path, _team_week("manifest-1"))
    payload = json.loads(path.read_text())
    payload["coverage"]["inferred_team_membership"] = count
    path.write_text(json.dumps(payload))

    with pytest.raises((HistoricalTeamWeekArtifactError, ReplayInputError)):
        load_historical_team_week_artifact(path)


def test_team_observations_require_aware_time_and_source_identity() -> None:
    with pytest.raises(ReplayInputError, match="timezone-aware"):
        PlayerTeamObservation("player", "team", datetime(2026, 1, 1), "fixture")
    with pytest.raises(ReplayInputError, match="identities"):
        PlayerTeamObservation("player", "team", datetime(2026, 1, 1), "")
