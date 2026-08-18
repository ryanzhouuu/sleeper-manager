import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from sleeper_manager.backtesting.experiments.data import (
    artifact_manifest,
    dataset_version_for,
    decision_cutoff,
    load_historical_experiment_inputs,
    scoring_policy_from_league_fixture,
)


class FakeTable:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def to_dict(self, *, orient: str) -> list[dict[str, Any]]:
        assert orient == "records"
        return self.rows


class ProjectableFakeTable(FakeTable):
    def __init__(
        self,
        rows: list[dict[str, Any]],
        projections: list[tuple[str, ...]],
    ) -> None:
        super().__init__(rows)
        self.columns = tuple(rows[0]) if rows else ()
        self.projections = projections

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, columns: list[str]) -> FakeTable:
        self.projections.append(tuple(columns))
        return FakeTable([{column: row.get(column) for column in columns} for row in self.rows])


def test_load_historical_inputs_filters_regular_season_and_adds_fouls(
    tmp_path: Path,
) -> None:
    season_dir = tmp_path / "2023"
    season_dir.mkdir()
    names = (
        "nba_schedule_2023.rds",
        "player_box_2023.rds",
        "team_box_2023.rds",
        "play_by_play_2023.rds",
    )
    for name in names:
        (season_dir / name).write_bytes(name.encode())
    played_at = datetime(2023, 1, 2, tzinfo=UTC)
    schedule = [
        {
            "game_id": "g1",
            "season_type": 2,
            "game_date_time": played_at,
            "home_id": "1",
            "away_id": "2",
            "status_type_name": "Final",
            "venue_id": "v1",
            "venue_address_city": "Chicago",
            "venue_address_state": "IL",
        },
        {
            "game_id": "playoff",
            "season_type": 3,
            "game_date_time": played_at,
            "home_id": "1",
            "away_id": "2",
            "status_type_name": "Final",
        },
    ]
    player = [
        {
            "game_id": "g1",
            "season_type": 2,
            "game_date_time": played_at,
            "athlete_id": "p1",
            "athlete_display_name": "Player One",
            "team_id": "1",
            "team_abbreviation": "CHI",
            "team_display_name": "Chicago Bulls",
            "team_location": "Chicago",
            "active": True,
            "points": 20,
            "rebounds": 10,
            "assists": 5,
            "steals": 1,
            "blocks": 0,
            "turnovers": 2,
            "three_point_field_goals_made": 3,
            "starter": True,
            "did_not_play": False,
            "minutes": 30,
        }
    ]
    team = [
        {
            "game_id": "g1",
            "season_type": 2,
            "game_date_time": played_at,
            "team_id": "1",
            "opponent_team_id": "2",
            "team_score": 100,
            "opponent_team_score": 90,
            "field_goals_attempted": 80,
            "free_throws_attempted": 20,
            "offensive_rebounds": 10,
            "total_turnovers": 12,
        }
    ]
    play = [
        {
            "game_id": "g1",
            "type_text": "Technical Foul",
            "athlete_id_1": "p1",
            "unused_payload": "discarded before record conversion",
        }
    ]
    tables: dict[str, list[dict[str, Any]]] = {
        "nba_schedule_2023.rds": schedule,
        "player_box_2023.rds": player,
        "team_box_2023.rds": team,
        "play_by_play_2023.rds": play,
    }

    projections: list[tuple[str, ...]] = []

    def reader(path: str) -> dict[str, FakeTable]:
        if Path(path).name == "play_by_play_2023.rds":
            return {"table": ProjectableFakeTable(play, projections)}
        return {"table": FakeTable(tables[Path(path).name])}

    result = load_historical_experiment_inputs(
        tmp_path,
        seasons=(2023,),
        retrieved_at=datetime(2026, 8, 14, tzinfo=UTC),
        rds_reader=reader,
    )

    assert [game.provider_id for game in result.games] == ["g1"]
    assert result.player_box_scores[0].line.technical_fouls == 1
    assert result.teams[0].abbreviation == "chi"
    assert result.provider_players[0].full_name == "Player One"
    assert len(result.artifacts) == 4
    assert result.excluded_player_rows == 0
    assert artifact_manifest(result.artifacts)[0]["sha256"]
    assert decision_cutoff(result.games[0]) == played_at - timedelta(minutes=30)
    assert projections == [("game_id", "type_text", "athlete_id_1")]


def test_scoring_and_dataset_versions_are_deterministic(tmp_path: Path) -> None:
    fixture = tmp_path / "league.json"
    fixture.write_text(json.dumps({"scoring_settings": {"pts": 1, "reb": 1.2}}))
    policy = scoring_policy_from_league_fixture(fixture)

    first = dataset_version_for((), scoring_policy=policy, injury_hashes=("b", "a"))
    second = dataset_version_for((), scoring_policy=policy, injury_hashes=("a", "b"))

    assert policy.points == 1
    assert policy.rebounds == 1.2
    assert first == second
    assert first.startswith("historical-features-v5-")


def test_load_historical_inputs_joins_schedule_periods_to_team_boxes(tmp_path: Path) -> None:
    season_dir = tmp_path / "2023"
    season_dir.mkdir()
    names = (
        "nba_schedule_2023.rds",
        "player_box_2023.rds",
        "team_box_2023.rds",
        "play_by_play_2023.rds",
    )
    for name in names:
        (season_dir / name).write_bytes(name.encode())

    starts = {
        "regulation": datetime(2023, 1, 2, tzinfo=UTC),
        "one_ot": datetime(2023, 1, 3, tzinfo=UTC),
        "two_ot": datetime(2023, 1, 4, tzinfo=UTC),
    }
    schedule = [
        {
            "game_id": game_id,
            "season_type": 2,
            "game_date_time": start,
            "home_id": "1",
            "away_id": "2",
            "status_type_name": "Final",
            "format_regulation_periods": 4,
            "status_period": periods,
        }
        for game_id, start, periods in (
            ("regulation", starts["regulation"], 4),
            ("one_ot", starts["one_ot"], 5),
            ("two_ot", starts["two_ot"], 6),
        )
    ]
    team = [
        {
            "game_id": game_id,
            "season_type": 2,
            "game_date_time": starts[key],
            "team_id": team_id,
            "opponent_team_id": opponent_id,
            "team_score": 100,
            "opponent_team_score": 95,
            "field_goals_attempted": 90,
            "free_throws_attempted": 20,
            "offensive_rebounds": 10,
            "total_turnovers": 12,
        }
        for key, game_id in (
            ("regulation", "regulation"),
            ("one_ot", "one_ot"),
            ("two_ot", "two_ot"),
        )
        for team_id, opponent_id in (("1", "2"), ("2", "1"))
    ]
    tables: dict[str, list[dict[str, Any]]] = {
        "nba_schedule_2023.rds": schedule,
        "player_box_2023.rds": [],
        "team_box_2023.rds": team,
        "play_by_play_2023.rds": [],
    }

    def reader(path: str) -> dict[str, FakeTable]:
        return {"table": FakeTable(tables[Path(path).name])}

    result = load_historical_experiment_inputs(
        tmp_path,
        seasons=(2023,),
        retrieved_at=datetime(2026, 8, 14, tzinfo=UTC),
        rds_reader=reader,
    )

    durations = {game.provider_id: game.duration_minutes for game in result.games}
    assert durations == {"regulation": 48, "one_ot": 53, "two_ot": 58}
    periods = {"regulation": 4, "one_ot": 5, "two_ot": 6}
    for team_box_score in result.team_box_scores:
        duration = durations[team_box_score.game_id]
        assert team_box_score.completed_periods == periods[team_box_score.game_id]
        assert team_box_score.pace_48 == pytest.approx(100.8 * 48 / duration)
