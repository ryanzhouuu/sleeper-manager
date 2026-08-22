from datetime import UTC, datetime

import pytest

from sleeper_manager.backtesting.replay.league_archive import (
    LeagueArchiveError,
    acquire_sleeper_archive,
    parse_historical_league_archive,
    resolve_predecessor_chain,
)


def league_payload() -> dict[str, object]:
    return {
        "league_id": "league-1",
        "sport": "nba",
        "season": "2025",
        "season_type": "regular",
        "status": "complete",
        "total_rosters": 1,
        "roster_positions": ["PG", "UTIL", "BN"],
        "scoring_settings": {"pts": 1, "reb": 1.2},
    }


def test_archive_parses_transaction_timestamps_and_positions() -> None:
    archive = parse_historical_league_archive(
        league_payload(),
        rosters=[{"roster_id": 1, "players": ["p1"], "starters": ["0", "p1"]}],
        matchup_weeks={
            1: [{"roster_id": 1, "players": ["p1"], "starters": ["0", "p1"]}]
        },
        transactions=[
            {
                "transaction_id": "tx-1",
                "type": "free_agent",
                "status": "complete",
                "created": 1_700_000_000_000,
                "status_updated": 1_700_000_001_000,
                "adds": {"p1": 1},
            }
        ],
        player_catalog={"p1": {"full_name": "Player One", "fantasy_positions": ["PG", "G"]}},
        retrieved_at=datetime(2026, 8, 16, tzinfo=UTC),
    )

    transaction = archive.transactions[0]
    assert transaction.status_updated_at == datetime.fromtimestamp(1_700_000_001, tz=UTC)
    assert archive.player_eligibility[0].eligible_positions == ("G", "PG")
    assert archive.matchup_weeks[0].week == 1
    assert archive.final_rosters[0].starter_ids == (None, "p1")
    assert archive.matchup_weeks[0].starter_ids == (None, "p1")


def test_archive_rejects_unsupported_nonzero_scoring_field() -> None:
    payload = league_payload()
    payload["scoring_settings"] = {"pts": 1, "blocks": 2}
    with pytest.raises(LeagueArchiveError, match="Unsupported nonzero"):
        parse_historical_league_archive(
            payload,
            rosters=[],
            retrieved_at=datetime(2026, 8, 16, tzinfo=UTC),
        )


def test_predecessor_resolution_is_deterministic_and_detects_cycles() -> None:
    payloads = {
        "shell": {"previous_league_id": "prior"},
        "prior": {"previous_league_id": None},
    }
    assert resolve_predecessor_chain("shell", payloads) == ("shell", "prior")
    with pytest.raises(LeagueArchiveError, match="cycle"):
        resolve_predecessor_chain(
            "a",
            {"a": {"previous_league_id": "b"}, "b": {"previous_league_id": "a"}},
        )


def test_archive_acquisition_is_immutable_and_hashes_raw_payloads(tmp_path) -> None:
    class Reader:
        async def league(self, league_id):
            return league_payload()

        async def rosters(self, league_id):
            return []

        async def matchups(self, league_id, week):
            return []

        async def transactions(self, league_id, week):
            return []

        async def players(self, *, active=True):
            return {}

    import asyncio

    artifacts = asyncio.run(
        acquire_sleeper_archive(
            Reader(),
            league_id="league-1",
            root=tmp_path,
            weeks=[1],
            retrieved_at=datetime(2026, 8, 16, tzinfo=UTC),
        )
    )
    assert artifacts[0].content_hash
    assert (tmp_path / "league-1" / "manifest.json").is_file()
