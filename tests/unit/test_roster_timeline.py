from datetime import UTC, date, datetime

from sleeper_manager.backtesting.league_archive import parse_historical_league_archive
from sleeper_manager.backtesting.roster_timeline import (
    assign_game_to_week,
    build_fantasy_week_boundaries,
    reconstruct_roster_timeline,
)


def _archive():
    return parse_historical_league_archive(
        {
            "league_id": "league-1",
            "sport": "nba",
            "season": "2025",
            "season_type": "regular",
            "status": "complete",
            "total_rosters": 1,
            "roster_positions": ["PG", "UTIL", "BN"],
            "scoring_settings": {"pts": 1},
        },
        rosters=[{"roster_id": 1, "players": ["p2"], "starters": ["p2"]}],
        transactions=[
            {
                "transaction_id": "tx-1",
                "type": "free_agent",
                "status": "complete",
                "status_updated": "2025-10-07T15:00:00Z",
                "adds": {"p2": 1},
                "drops": {"p1": 1},
            }
        ],
        retrieved_at=datetime(2026, 8, 16, tzinfo=UTC),
    )


def test_timeline_reverse_reconstructs_initial_membership_and_replays_drop_add() -> None:
    weeks = build_fantasy_week_boundaries({1: date(2025, 10, 6), 2: date(2025, 10, 13)})
    timeline = reconstruct_roster_timeline(_archive(), week_boundaries=weeks)

    assert timeline.membership_at(1, "p1", datetime(2025, 10, 7, 14, tzinfo=UTC))
    assert not timeline.membership_at(1, "p1", datetime(2025, 10, 7, 16, tzinfo=UTC))
    assert timeline.membership_at(1, "p2", datetime(2025, 10, 7, 16, tzinfo=UTC))
    assert timeline.exclusions == ()


def test_week_assignment_uses_eastern_monday_boundary_for_late_sunday() -> None:
    weeks = build_fantasy_week_boundaries({1: date(2025, 10, 6), 2: date(2025, 10, 13)})
    late_sunday = datetime(2025, 10, 13, 3, 30, tzinfo=UTC)
    assert assign_game_to_week(late_sunday, weeks) is weeks[0]


def test_missing_effective_time_is_explicitly_excluded() -> None:
    archive = parse_historical_league_archive(
        {
            "league_id": "league-1",
            "sport": "nba",
            "season": "2025",
            "season_type": "regular",
            "status": "complete",
            "total_rosters": 1,
            "roster_positions": ["PG"],
            "scoring_settings": {"pts": 1},
        },
        rosters=[{"roster_id": 1, "players": ["p1"]}],
        transactions=[
            {
                "transaction_id": "tx-missing",
                "type": "free_agent",
                "status": "complete",
                "adds": {"p1": 1},
            }
        ],
        retrieved_at=datetime(2026, 8, 16, tzinfo=UTC),
    )
    timeline = reconstruct_roster_timeline(
        archive,
        season_start=datetime(2025, 10, 1, tzinfo=UTC),
        season_end=datetime(2025, 10, 8, tzinfo=UTC),
    )
    assert any(reason.startswith("missing_effective_timestamp") for reason in timeline.exclusions)
