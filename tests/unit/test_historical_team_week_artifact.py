"""Fail-closed file and relationship validation for team-week artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from historical_team_week_artifact_support import _manifest, _team_week, _write_lone_payload

from sleeper_manager.backtesting.artifacts import canonical_json_bytes
from sleeper_manager.backtesting.replay.inputs import (
    HistoricalTeamWeekArtifactError,
    load_historical_team_week_artifact,
    write_replay_input_bundle,
)


def test_load_reconstructs_canonical_serialized_fixture(tmp_path: Path) -> None:
    """Round-trip a written bundle through the explicit artifact loader."""

    team_week = _team_week(manifest_id="pending")
    manifest = _manifest()
    team_week = _team_week(manifest_id=manifest.manifest_id)
    bundle = write_replay_input_bundle(tmp_path / "inputs", manifest, (team_week,))
    path = bundle / "team-weeks/league-1/week-01/roster-1.json"

    loaded = load_historical_team_week_artifact(path)

    assert loaded == team_week
    assert path.read_bytes() == canonical_json_bytes(loaded.to_dict())


def test_load_rejects_missing_manifest(tmp_path: Path) -> None:
    """Canonical layout without a neighboring manifest fails closed."""

    path = (
        tmp_path / "inputs" / "deadbeef" / "team-weeks" / "league-1" / "week-01" / "roster-1.json"
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_json_bytes(_team_week("deadbeef").to_dict()))

    with pytest.raises(HistoricalTeamWeekArtifactError, match="manifest"):
        load_historical_team_week_artifact(path)


def test_load_rejects_mismatched_manifest_id(tmp_path: Path) -> None:
    """Payload and enclosing manifest must agree on the content hash identity."""

    manifest = _manifest()
    team_week = _team_week(manifest_id=manifest.manifest_id)
    bundle = write_replay_input_bundle(tmp_path / "inputs", manifest, (team_week,))
    path = bundle / "team-weeks/league-1/week-01/roster-1.json"
    payload = json.loads(path.read_text())
    payload["manifest_id"] = "other-manifest"
    path.write_text(json.dumps(payload))

    with pytest.raises(HistoricalTeamWeekArtifactError, match="manifest"):
        load_historical_team_week_artifact(path)


def test_load_rejects_cross_record_identity_mismatches(tmp_path: Path) -> None:
    """Nested player-game evidence must identify its enclosing player and game."""

    path = _write_lone_payload(tmp_path, _team_week("manifest-1"))
    payload = json.loads(path.read_text())
    payload["player_games"][0]["projection"]["player_id"] = "other-player"
    path.write_text(json.dumps(payload))

    with pytest.raises(HistoricalTeamWeekArtifactError, match="projection identity"):
        load_historical_team_week_artifact(path)

    path = _write_lone_payload(tmp_path / "unknown-game", _team_week("manifest-1"))
    payload = json.loads(path.read_text())
    payload["player_games"][0]["game_id"] = "unknown-game"
    payload["player_games"][0]["projection"]["game_id"] = "unknown-game"
    path.write_text(json.dumps(payload))

    with pytest.raises(HistoricalTeamWeekArtifactError, match="unknown game"):
        load_historical_team_week_artifact(path)


def test_load_rejects_malformed_json_and_non_object(tmp_path: Path) -> None:
    """Malformed JSON and non-object roots fail closed."""

    path = tmp_path / "broken.json"
    path.write_text("{")
    with pytest.raises(HistoricalTeamWeekArtifactError, match="JSON"):
        load_historical_team_week_artifact(path)

    path = tmp_path / "array.json"
    path.write_text("[]")
    with pytest.raises(HistoricalTeamWeekArtifactError, match="object"):
        load_historical_team_week_artifact(path)
