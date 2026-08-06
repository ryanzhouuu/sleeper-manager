import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from sleeper_manager.integrations.nba.historical import (
    SportsDataverseClient,
    SportsDataverseError,
    parse_player_box_score_rows,
    parse_schedule_rows,
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
