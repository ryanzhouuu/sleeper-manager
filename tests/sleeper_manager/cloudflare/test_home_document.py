from datetime import UTC, datetime

import pytest

from sleeper_manager.cloudflare.home_document import (
    GameRow,
    HomeSide,
    RosterRow,
    assemble_home,
    avatar_image_url,
    label_team_games,
    plan_status_from_trace,
    player_image_url,
    team_image_url,
)
from sleeper_manager.domain.nba import GameStatus, ScheduledGame, SourceMetadata
from sleeper_manager.domain.scheduling import eastern_fantasy_week

NOW = datetime(2026, 10, 20, 18, tzinfo=UTC)
TIPOFF = datetime(2026, 10, 21, 23, tzinfo=UTC)


def _game(
    provider_id: str,
    start: datetime,
    *,
    status: GameStatus = GameStatus.SCHEDULED,
    home: str | None = "NY",
    away: str | None = "BOS",
) -> ScheduledGame:
    return ScheduledGame(
        provider_id=provider_id,
        start_time=start,
        status=status,
        home_team_id="18",
        away_team_id="2",
        status_detail=None,
        source=SourceMetadata(provider="espn", provider_id=provider_id, retrieved_at=NOW),
        home_abbreviation=home,
        away_abbreviation=away,
    )


def test_team_image_url_uses_espn_slug() -> None:
    assert team_image_url("NOP") == "https://a.espncdn.com/i/teamlogos/nba/500/no.png"
    assert team_image_url("UTA") == "https://a.espncdn.com/i/teamlogos/nba/500/utah.png"
    assert team_image_url("NYK") == "https://a.espncdn.com/i/teamlogos/nba/500/nyk.png"
    assert team_image_url(None) is None
    assert team_image_url("  ") is None


def test_player_and_avatar_urls_are_null_without_ids() -> None:
    assert (
        player_image_url("3934672")
        == "https://a.espncdn.com/i/headshots/nba/players/full/3934672.png"
    )
    assert player_image_url(None) is None
    assert avatar_image_url("abc") == "https://sleepercdn.com/avatars/thumbs/abc"
    assert avatar_image_url(None) is None


def test_label_team_games_filters_the_eastern_week_and_postponements() -> None:
    window = eastern_fantasy_week(NOW)
    labeled = label_team_games(
        "NYK",
        (
            _game("in", TIPOFF, home="NY", away="BOS"),
            _game("late", datetime(2026, 10, 26, 4, tzinfo=UTC), home="NY", away="MIA"),
            _game("post", TIPOFF, status=GameStatus.POSTPONED, home="NY", away="PHI"),
        ),
        window,
    )

    assert labeled is not None
    assert len(labeled) == 1
    assert labeled[0].opponent == "BOS"
    assert labeled[0].home is True


def test_label_team_games_returns_none_when_a_week_game_has_no_abbreviation() -> None:
    labeled = label_team_games(
        "NYK",
        (_game("blank", TIPOFF, home=None, away=None),),
        eastern_fantasy_week(NOW),
    )
    assert labeled is None


def test_assemble_home_allowlist_and_null_score() -> None:
    document = assemble_home(
        league_name="Lock In",
        week=3,
        timezone_name="America/Chicago",
        manager=HomeSide(name="Home Team", score=None, image_url=None),
        opponent=None,
        roster=(
            RosterRow(slot="PG", name=None, nba_team="NYK", espn_id=None),
            RosterRow(slot="BN", name="Bench Player", nba_team=None, espn_id="10"),
        ),
        games_status="empty",
        games=(),
        recommendation_status="ready",
        recommendation_plan_status="no_action",
        recommendation_title="Lineup is set",
        recommendation_message="Stored message",
        recommendation_deadline=TIPOFF,
    )

    assert document["matchup"]["opponent"] is None
    assert document["matchup"]["manager"]["score"] is None
    assert document["roster"][0]["name"] is None
    assert document["roster"][0]["image_url"] is None
    assert document["roster"][0]["team_image_url"].endswith("/nyk.png")
    assert document["roster"][1]["image_url"].endswith("/10.png")
    assert document["games"] == {"status": "empty", "items": []}
    assert document["recommendation"]["plan_status"] == "no_action"
    assert document["recommendation"]["title"] == "Lineup is set"
    assert document["recommendation"]["deadline"] == TIPOFF.isoformat()
    assert "league_id" not in document["league"]
    assert "user_id" not in str(document)
    assert "token" not in str(document)
    assert "trace" not in str(document)


def test_assemble_home_game_row_uses_player_image_fields() -> None:
    document = assemble_home(
        league_name="Lock In",
        week=3,
        timezone_name="America/Chicago",
        manager=HomeSide(
            name="Home Team",
            score=12.5,
            image_url="https://sleepercdn.com/avatars/thumbs/abc",
        ),
        opponent=HomeSide(name="Away Team", score=4, image_url=None),
        roster=(),
        games_status="ready",
        games=(
            GameRow(
                start=TIPOFF,
                player_name="Jalen Brunson",
                espn_id="3934672",
                opponent="BOS",
                home=False,
            ),
        ),
        recommendation_status="empty",
    )

    item = document["games"]["items"][0]
    assert item["player_name"] == "Jalen Brunson"
    assert item["home"] is False
    assert item["opponent"] == "BOS"
    assert item["opponent_image_url"].endswith("/bos.png")
    assert item["start"] == TIPOFF.isoformat()
    assert document["recommendation"] == {"status": "empty"}
    assert set(document) == {"league", "matchup", "roster", "games", "recommendation"}


@pytest.mark.parametrize(
    ("trace_json", "expected"),
    [
        ('{"status": "action_required"}', "action_required"),
        ('{"status": "blocked", "kind": "weekly_lineup"}', "blocked"),
        ("{}", None),
        ('{"status": "pending"}', None),
        ('{"status": 1}', None),
        ("[]", None),
        ("not json", None),
    ],
)
def test_plan_status_from_trace(trace_json: str, expected: str | None) -> None:
    assert plan_status_from_trace(trace_json) == expected
