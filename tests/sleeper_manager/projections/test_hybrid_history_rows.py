"""Source presence, explicit timing and participation at the raw-history boundary."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sleeper_manager.backtesting.artifacts import atomic_write_json
from sleeper_manager.domain.nba import GameStatus, ScheduledGame, SourceMetadata
from sleeper_manager.projections.hybrid_config import HybridProjectionError
from sleeper_manager.projections.hybrid_history_artifact import load_hybrid_history_artifact
from sleeper_manager.projections.hybrid_history_rows import (
    HistoryFinalization,
    parse_hybrid_history_rows,
)
from sleeper_manager.projections.hybrid_types import HybridHistory

START = datetime(2025, 1, 1, 23, tzinfo=UTC)
FINAL = START + timedelta(hours=3)
GAME = ScheduledGame(
    "100",
    START,
    GameStatus.FINAL,
    "1",
    "2",
    None,
    SourceMetadata("fixture", "100", FINAL),
)
TIMING = {"100": HistoryFinalization(FINAL, "verified-final-v1")}


def raw_row(**changes: object) -> dict[str, object]:
    return {
        "game_id": 100.0,
        "athlete_id": 200.0,
        "team_id": 1.0,
        "season_type": 2.0,
        "did_not_play": False,
        "minutes": 30.5,
        "points": 20.0,
        "rebounds": 7.0,
        "assists": 3.0,
        "steals": 0.0,
        "blocks": 0.0,
        "turnovers": 2.0,
        "three_point_field_goals_made": 4.0,
        **changes,
    }


def parse(rows: list[dict[str, object]], **changes: object):  # type: ignore[no-untyped-def]
    kwargs = dict(
        games=(GAME,),
        resolved_player_ids=("200", "201"),
        finalization=TIMING,
        source_version="raw-file-sha256",
    )
    kwargs.update(changes)
    return parse_hybrid_history_rows(rows, **kwargs)  # type: ignore[arg-type]


def test_source_zeros_are_verified_and_absent_fouls_are_unverified(tmp_path: Path) -> None:
    parsed = parse([raw_row()])
    row = parsed.observations[0]
    assert row.player_id == "200"
    assert row.line.steals == 0 and "steals" in row.verified_fields
    assert row.line.three_pointers_made == 4
    assert "technical_fouls" not in row.verified_fields
    assert parsed.gaps[0].fields == ("flagrant_fouls", "technical_fouls")
    history = HybridHistory("fixture", parsed.observations, ())
    path = tmp_path / "history.json"
    atomic_write_json(path, history)
    assert load_hybrid_history_artifact(path) == history


@pytest.mark.parametrize("missing", [None, float("nan")])
def test_missing_core_stats_and_minutes_remain_gaps(missing: object) -> None:
    parsed = parse([raw_row(assists=missing, minutes=missing)])
    row = parsed.observations[0]
    assert "assists" not in row.verified_fields
    assert row.minutes is None
    assert {gap.reason for gap in parsed.gaps} == {
        "missing_stat_fields",
        "missing_positive_minutes",
    }
    assert "assists" in next(
        gap.fields for gap in parsed.gaps if gap.reason == "missing_stat_fields"
    )


def test_explicit_foul_values_gain_coverage_without_pbp_inference() -> None:
    row = parse([raw_row(technical_fouls=0.0, flagrant_fouls=1.0)]).observations[0]
    assert {"technical_fouls", "flagrant_fouls"} <= set(row.verified_fields)
    assert row.line.flagrant_fouls == 1


def test_dnp_supplies_only_an_explicit_outcome() -> None:
    parsed = parse([raw_row(did_not_play=True, minutes=None)])
    assert parsed.observations == ()
    assert parsed.outcomes[0].did_play is False
    assert parsed.gaps == ()


def test_missing_participation_is_not_assumed_played() -> None:
    parsed = parse([raw_row(did_not_play=None)])
    assert not parsed.outcomes and not parsed.observations
    assert parsed.gaps[0].reason == "missing_participation"


def test_missing_finalization_never_uses_game_end_or_retrieval_time() -> None:
    parsed = parse([raw_row()], finalization={})
    assert not parsed.observations
    assert parsed.outcomes[0].did_play
    assert "missing_finalization" in {gap.reason for gap in parsed.gaps}


def test_unknown_player_and_missing_identity_are_reported() -> None:
    assert parse([raw_row()], resolved_player_ids=()).gaps[0].reason == "unresolved_player"
    assert parse([raw_row(athlete_id=None)]).gaps[0].reason == "missing_source_identity"


def test_nonregular_rows_are_ignored_and_unknown_games_are_gaps() -> None:
    assert not parse([raw_row(season_type=3)]).outcomes
    assert parse([raw_row(game_id=101)]).gaps[0].reason == "missing_final_game"


@pytest.mark.parametrize(
    "field,value",
    [
        ("points", 20.9),
        ("rebounds", -1),
        ("steals", True),
        ("points", "20"),
        ("blocks", float("inf")),
        ("minutes", -1),
        ("did_not_play", "false"),
        ("season_type", True),
        ("athlete_id", 200.5),
    ],
)
def test_invalid_source_values_are_rejected(field: str, value: object) -> None:
    with pytest.raises(HybridProjectionError):
        parse([raw_row(**{field: value})])


def test_duplicate_rows_and_conflicting_teams_are_rejected() -> None:
    with pytest.raises(HybridProjectionError, match="duplicate_player_game"):
        parse([raw_row(), raw_row()])
    with pytest.raises(HybridProjectionError, match="outcome_team_mismatch"):
        parse([raw_row(team_id="3")])


def test_finalization_requires_aware_compatible_source_evidence() -> None:
    with pytest.raises(HybridProjectionError, match="naive_timestamp"):
        HistoryFinalization(FINAL.replace(tzinfo=None), "v1")
    with pytest.raises(HybridProjectionError, match="missing_finalization_source"):
        HistoryFinalization(FINAL, " ")
    with pytest.raises(HybridProjectionError, match="finalization_before_game"):
        parse(
            [raw_row()],
            finalization={"100": HistoryFinalization(START - timedelta(seconds=1), "v1")},
        )
    with pytest.raises(HybridProjectionError, match="finalization_without_final_game"):
        parse([raw_row()], games=(replace(GAME, status=GameStatus.SCHEDULED),))
    with pytest.raises(HybridProjectionError, match="duplicate_history_game"):
        parse([raw_row()], games=(GAME, GAME))


def test_row_order_is_stable_and_finalization_policy_is_bound() -> None:
    rows = [raw_row(), raw_row(athlete_id=201)]
    assert parse(rows) == parse(list(reversed(rows)))
    changed = parse(rows, finalization={"100": HistoryFinalization(FINAL, "different-policy")})
    assert changed.observations[0].source_version != parse(rows).observations[0].source_version
