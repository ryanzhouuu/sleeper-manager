"""Fail-closed schema decoding for persisted historical team-week evidence."""

from __future__ import annotations

import pytest
from historical_team_week_artifact_support import _team_week

from sleeper_manager.backtesting.replay.inputs._artifact_decoding import (
    HistoricalTeamWeekArtifactError,
    decode_historical_team_week,
)


def test_decode_reconstructs_canonical_serialized_payload() -> None:
    """Reconstruct the complete typed team-week from its canonical dictionary."""

    team_week = _team_week("manifest-1")

    assert decode_historical_team_week(team_week.to_dict()) == team_week


def test_decode_rejects_malformed_enum_and_timestamp() -> None:
    """Invalid status enums and naive timestamps are rejected."""

    payload = _team_week("manifest-1").to_dict()
    payload["games"][0]["status"] = "not-a-status"
    with pytest.raises(HistoricalTeamWeekArtifactError, match="status"):
        decode_historical_team_week(payload)

    payload = _team_week("manifest-1").to_dict()
    payload["games"][0]["start_time"] = "2026-02-02T19:30:00"
    with pytest.raises(HistoricalTeamWeekArtifactError, match="timezone"):
        decode_historical_team_week(payload)


def test_decode_rejects_absent_decision_critical_fields() -> None:
    """Missing nested coverage or projection fields do not silently default."""

    payload = _team_week("manifest-1").to_dict()
    del payload["coverage"]["projected_player_games"]
    with pytest.raises(HistoricalTeamWeekArtifactError, match="projected_player_games"):
        decode_historical_team_week(payload)

    payload = _team_week("manifest-1").to_dict()
    del payload["player_games"][0]["projection"]["available_as_of"]
    with pytest.raises(HistoricalTeamWeekArtifactError, match="available_as_of"):
        decode_historical_team_week(payload)
