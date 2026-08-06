import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sleeper_manager.domain.nba import AvailabilityStatus, GameStatus
from sleeper_manager.integrations.nba.espn import (
    ESPNSchemaError,
    parse_game_summary,
    parse_injuries,
    parse_scoreboard,
    parse_team_roster,
    parse_team_schedule,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "espn"
RETRIEVED_AT = datetime(2026, 8, 4, 4, tzinfo=UTC)


def fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_scoreboard_normalizes_statuses_and_utc_times() -> None:
    result = parse_scoreboard(fixture("scoreboard.json"), retrieved_at=RETRIEVED_AT)

    assert [game.status for game in result.records] == [
        GameStatus.SCHEDULED,
        GameStatus.IN_PROGRESS,
        GameStatus.FINAL,
    ]
    assert result.records[0].start_time.tzinfo is UTC
    assert result.quality.record_count == 3


def test_scoreboard_normalizes_postponed_and_canceled_games() -> None:
    result = parse_scoreboard(fixture("status_edges.json"), retrieved_at=RETRIEVED_AT)

    assert [game.status for game in result.records] == [
        GameStatus.POSTPONED,
        GameStatus.CANCELED,
    ]


def test_empty_scoreboard_is_a_valid_empty_result() -> None:
    result = parse_scoreboard({"events": []}, retrieved_at=RETRIEVED_AT)

    assert result.records == ()
    assert result.quality.state.value == "empty"


def test_summary_normalizes_box_score_and_did_not_play() -> None:
    result = parse_game_summary(fixture("summary.json"), retrieved_at=RETRIEVED_AT)

    first, second = result.records.player_box_scores
    assert first.line.points == 28
    assert first.line.three_pointers_made == 2
    assert first.minutes == pytest.approx(34.5)
    assert first.did_play
    assert not second.did_play
    assert second.minutes is None


def test_injuries_normalize_availability() -> None:
    result = parse_injuries(fixture("injuries.json"), retrieved_at=RETRIEVED_AT)

    assert [record.status for record in result.records] == [
        AvailabilityStatus.QUESTIONABLE,
        AvailabilityStatus.OUT,
    ]


def test_roster_and_schedule_are_canonical() -> None:
    roster = parse_team_roster(
        fixture("roster.json"), team_id="team-home", retrieved_at=RETRIEVED_AT
    )
    schedule = parse_team_schedule(fixture("schedule.json"), retrieved_at=RETRIEVED_AT)

    assert roster.records[0].provider_id == "espn-1"
    assert roster.records[0].team_abbreviation == "DEN"
    assert schedule.records[0].status is GameStatus.FINAL


def test_malformed_scoreboard_is_an_explicit_schema_error() -> None:
    with pytest.raises(ESPNSchemaError, match="competitions"):
        parse_scoreboard(
            {"events": [{"id": "missing-fields", "date": "2026-08-04T00:00:00Z"}]},
            retrieved_at=RETRIEVED_AT,
        )
