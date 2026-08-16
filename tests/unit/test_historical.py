import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from sleeper_manager.integrations.nba.historical import (
    SportsDataverseClient,
    SportsDataverseError,
    apply_player_game_fouls,
    parse_play_by_play_foul_rows,
    parse_player_box_score_rows,
    parse_schedule_rows,
    parse_team_box_score_rows,
    player_box_score_url,
)

RETRIEVED_AT = datetime(2026, 8, 4, 4, tzinfo=UTC)


def test_historical_box_scores_match_live_canonical_shape() -> None:
    result = parse_player_box_score_rows(
        [
            {
                "game_id": "game-1",
                "athlete_id": 101,
                "team_id": "team-1",
                "game_date": "2026-01-01T02:00:00Z",
                "minutes": 32.5,
                "started": True,
                "points": 25,
                "rebounds": 10,
                "assists": 8,
                "steals": 2,
                "blocks": 1,
                "turnovers": 3,
                "three_pointers_made": 4,
            }
        ],
        retrieved_at=RETRIEVED_AT,
    )

    record = result.records[0]
    assert record.player_id == "101"
    assert record.line.points == 25
    assert record.source.provider == "sportsdataverse"
    assert record.played_at is not None and record.played_at.tzinfo is UTC


def test_historical_schedule_normalizes_status_and_teams() -> None:
    result = parse_schedule_rows(
        [
            {
                "game_id": "game-1",
                "game_date": "2026-01-01",
                "home_team_id": "home",
                "away_team_id": "away",
                "status": "final",
            }
        ],
        retrieved_at=RETRIEVED_AT,
    )

    assert result.records[0].home_team_id == "home"
    assert result.records[0].status.value == "final"


def test_release_schedule_schema_preserves_tipoff_and_venue() -> None:
    result = parse_schedule_rows(
        [
            {
                "game_id": "game-1",
                "game_date": "2026-01-01",
                "game_date_time": "2026-01-02T01:30:00Z",
                "home_id": "home",
                "away_id": "away",
                "status_type_name": "STATUS_FINAL",
                "venue_id": "123",
                "venue_full_name": "Arena",
                "venue_address_city": "Chicago",
                "venue_address_state": "IL",
                "neutral_site": False,
            }
        ],
        retrieved_at=RETRIEVED_AT,
    )

    game = result.records[0]
    assert game.start_time == datetime(2026, 1, 2, 1, 30, tzinfo=UTC)
    assert game.venue_id == "123"
    assert game.venue_city == "Chicago"


def test_schedule_and_team_box_parsers_preserve_period_metadata() -> None:
    schedule = parse_schedule_rows(
        [
            {
                "game_id": "ot-game",
                "game_date": "2026-01-02T01:30:00Z",
                "home_id": "home",
                "away_id": "away",
                "format_regulation_periods": 4,
                "status_period": 6,
            }
        ],
        retrieved_at=RETRIEVED_AT,
    ).records[0]
    team_box = parse_team_box_score_rows(
        [
            {
                "game_id": "ot-game",
                "game_date": "2026-01-02T01:30:00Z",
                "team_id": "home",
                "opponent_team_id": "away",
                "field_goals_attempted": 90,
                "free_throws_attempted": 20,
                "offensive_rebounds": 10,
                "total_turnovers": 12,
                "format_regulation_periods": 4,
                "status_period": 6,
            }
        ],
        retrieved_at=RETRIEVED_AT,
    ).records[0]

    assert schedule.duration_minutes == 58
    assert team_box.pace_48 == pytest.approx(100.8 * 48 / 58)


def test_team_boxes_supply_prior_only_possession_inputs() -> None:
    result = parse_team_box_score_rows(
        [
            {
                "game_id": "game-1",
                "game_date_time": "2026-01-02T01:30:00Z",
                "team_id": "home",
                "opponent_team_id": "away",
                "team_score": 110,
                "opponent_team_score": 100,
                "field_goals_attempted": 90,
                "free_throws_attempted": 20,
                "offensive_rebounds": 10,
                "total_turnovers": 12,
            }
        ],
        retrieved_at=RETRIEVED_AT,
    )

    record = result.records[0]
    assert record.points == 110
    assert record.estimated_possessions == pytest.approx(100.8)


def test_play_by_play_fouls_enrich_player_box_scores() -> None:
    fouls = parse_play_by_play_foul_rows(
        [
            {
                "game_id": "game-1",
                "type_text": "Double Technical Foul",
                "athlete_id_1": "101",
                "athlete_id_2": "102",
            },
            {
                "game_id": "game-1",
                "type_text": "Flagrant Foul Type 1",
                "athlete_id_1": "101",
            },
            {
                "game_id": "game-1",
                "type_text": "Free Throw - Technical",
                "athlete_id_1": "103",
            },
        ],
        retrieved_at=RETRIEVED_AT,
    )
    box_scores = parse_player_box_score_rows(
        [
            {
                "game_id": "game-1",
                "athlete_id": "101",
                "team_id": "home",
                "game_date_time": "2026-01-02T01:30:00Z",
                "points": 10,
            }
        ],
        retrieved_at=RETRIEVED_AT,
    )

    enriched = apply_player_game_fouls(box_scores.records, fouls.records)

    assert enriched[0].line.technical_fouls == 1
    assert enriched[0].line.flagrant_fouls == 1
    assert enriched[0].additional_sources == (fouls.records[0].source,)


def test_historical_schema_errors_identify_missing_columns() -> None:
    with pytest.raises(SportsDataverseError, match="player_id"):
        parse_player_box_score_rows(
            [{"game_id": "game-1", "team_id": "team-1", "game_date": "2026-01-01"}],
            retrieved_at=RETRIEVED_AT,
        )


def test_historical_client_downloads_and_parses_a_release() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == player_box_score_url(2025)
        return httpx.Response(200, content=b"public fixture")

    def read_rds(path: str) -> dict[str, list[dict[str, object]]]:
        assert path.endswith(".rds")
        return {
            "player_box_scores": [
                {
                    "game_id": "game-1",
                    "athlete_id": 101,
                    "team_id": "team-1",
                    "game_date": "2026-01-01",
                    "points": 10,
                }
            ]
        }

    async def run() -> int:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            async with SportsDataverseClient(
                client=client,
                rds_reader=read_rds,
                clock=lambda: RETRIEVED_AT,
            ) as adapter:
                result = await adapter.player_box_scores(2025)
                return result.records[0].line.points

    assert asyncio.run(run()) == 10
