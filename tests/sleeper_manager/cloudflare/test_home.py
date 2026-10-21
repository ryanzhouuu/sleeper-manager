import asyncio
from datetime import UTC, datetime, timedelta

from sleeper_manager.cloudflare.home import build_home, roster_rows
from sleeper_manager.cloudflare.home_roster import roster_standings
from sleeper_manager.cloudflare.providers import CloudflareProviderError
from sleeper_manager.domain.nba import (
    DataQualityReport,
    DataQualityState,
    GameStatus,
    ProviderPlayer,
    ProviderResult,
    ScheduledGame,
    SourceMetadata,
    quality_for_records,
)
from sleeper_manager.integrations.nba.espn import ESPNSchemaError
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import RecommendationRecord

NOW = datetime(2026, 10, 20, 18, tzinfo=UTC)
TIPOFF = datetime(2026, 10, 21, 23, tzinfo=UTC)


class FakeSleeper:
    def __init__(self) -> None:
        self.matchup_calls = 0
        self.player_calls = 0
        self.fail_players = False
        self.opponent_owner: str | None = "user-2"
        self.manager_players = ["known", "missing"]
        self.manager_starters = ["known"]
        self.state_payload: dict[str, object] = {
            "week": 3,
            "season": "2026",
            "season_type": "regular",
        }
        self.matchup_payload: list[dict[str, object]] = [
            {"roster_id": 1, "matchup_id": 7, "points": True},
            {"roster_id": 2, "matchup_id": 7, "points": 9},
        ]
        self.manager_settings: dict[str, object] | None = {
            "wins": 2,
            "losses": 1,
            "ties": 0,
            "fpts": 812,
            "fpts_decimal": 4,
        }
        self.catalog: dict[str, dict[str, object]] = {
            "known": {
                "full_name": "Known Player",
                "team": "NOP",
                "espn_id": "55",
                "fantasy_positions": ["SG", "C", "PG", "G"],
                "injury_status": "DTD",
            },
            "unparsed": {"full_name": "", "team": "BOS"},
        }

    async def league(self, league_id: str) -> dict[str, object]:
        return {
            "league_id": league_id,
            "name": "Lock In",
            "sport": "nba",
            "season": "2026",
            "season_type": "regular",
            "status": "in_season",
            "total_rosters": 2,
            "roster_positions": ["PG", "BN"],
            "scoring_settings": {},
            "settings": {},
        }

    async def users(self, league_id: str) -> list[dict[str, object]]:
        del league_id
        return [
            {
                "user_id": "user-1",
                "display_name": "Manager",
                "metadata": {"team_name": "Home Team"},
                "avatar": "abc",
            },
            {
                "user_id": "user-2",
                "display_name": "Opponent",
                "metadata": {"team_name": "Away Team"},
            },
        ]

    async def rosters(self, league_id: str) -> list[dict[str, object]]:
        del league_id
        return [
            {
                "roster_id": 1,
                "owner_id": "user-1",
                "players": list(self.manager_players),
                "starters": list(self.manager_starters),
                "reserve": [],
                "settings": self.manager_settings,
            },
            {
                "roster_id": 2,
                "owner_id": self.opponent_owner,
                "players": ["other"],
                "starters": ["other"],
                "reserve": [],
                "settings": {"wins": 1, "losses": 1, "ties": 1, "fpts": 700, "fpts_decimal": 50},
            },
        ]

    async def state(self) -> dict[str, object]:
        return dict(self.state_payload)

    async def matchups(self, league_id: str, week: int) -> list[dict[str, object]]:
        del league_id, week
        self.matchup_calls += 1
        return list(self.matchup_payload)

    async def players(self, *, active: bool = True) -> dict[str, dict[str, object]]:
        del active
        self.player_calls += 1
        if self.fail_players:
            raise CloudflareProviderError("catalog down")
        return self.catalog


class FakeSchedule:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.roster_calls: list[str] = []
        self.fail_teams: set[str] = set()
        self.error_teams: set[str] = set()
        self.starts = (TIPOFF,)
        self.schedule_error: BaseException | None = None
        self.roster_error: BaseException | None = None
        self.roster_players: tuple[ProviderPlayer, ...] = ()

    async def team_schedule(
        self, team_id: str, season: int
    ) -> ProviderResult[tuple[ScheduledGame, ...]]:
        self.calls.append(team_id)
        assert season == 2027
        if self.schedule_error is not None:
            raise self.schedule_error
        if team_id in self.fail_teams:
            raise CloudflareProviderError(f"{team_id} down")
        games = tuple(
            ScheduledGame(
                provider_id=f"{team_id}-{index}",
                start_time=start,
                status=GameStatus.SCHEDULED,
                home_team_id="1",
                away_team_id="2",
                status_detail=None,
                source=SourceMetadata(provider="espn", provider_id=team_id, retrieved_at=NOW),
                home_abbreviation="NO",
                away_abbreviation="BOS",
            )
            for index, start in enumerate(self.starts)
        )
        if team_id in self.error_teams:
            quality = DataQualityReport(
                state=DataQualityState.ERROR,
                resource="team_schedule",
                record_count=0,
                retrieved_at=NOW,
                source_updated_at=None,
                expires_at=None,
            )
            return ProviderResult(records=(), quality=quality)
        return ProviderResult(
            records=games,
            quality=quality_for_records(
                resource="team_schedule",
                records=games,
                retrieved_at=NOW,
                expires_at=NOW + timedelta(hours=1),
            ),
        )

    async def team_roster(self, team_id: str) -> ProviderResult[tuple[ProviderPlayer, ...]]:
        self.roster_calls.append(team_id)
        if self.roster_error is not None:
            raise self.roster_error
        return ProviderResult(
            records=self.roster_players,
            quality=quality_for_records(
                resource="team_roster",
                records=self.roster_players,
                retrieved_at=NOW,
                expires_at=NOW + timedelta(hours=1),
            ),
        )


def _repository(tmp_path):  # type: ignore[no-untyped-def]
    repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
    asyncio.run(repository.initialize())
    return repository


def _build(sleeper: FakeSleeper, schedule: FakeSchedule, repository: AsyncSQLiteStateRepository):  # type: ignore[no-untyped-def]
    return asyncio.run(
        build_home(
            sleeper=sleeper,
            espn=schedule,
            repository=repository,
            league_id="league-1",
            user_id="user-1",
            timezone_name="America/Chicago",
            now=NOW,
        )
    )


def test_build_home_returns_roster_with_null_score_and_missing_player(tmp_path) -> None:  # type: ignore[no-untyped-def]
    response = _build(FakeSleeper(), FakeSchedule(), _repository(tmp_path))

    assert response.status_code == 200
    document = response.payload
    assert [row["slot"] for row in document["roster"]] == ["PG", "BN"]
    assert document["roster"][0]["name"] == "Known Player"
    assert document["roster"][1]["name"] is None
    assert document["matchup"]["manager"]["score"] is None
    assert document["matchup"]["manager"]["image_url"] == (
        "https://sleepercdn.com/avatars/thumbs/abc"
    )
    assert document["matchup"]["opponent"]["score"] == 9.0
    assert [item["player_name"] for item in document["games"]["items"]] == ["Known Player"]
    assert document["games"]["items"][0]["home"] is True
    assert document["games"]["items"][0]["opponent"] == "BOS"
    assert document["recommendation"] == {"status": "empty"}
    rendered = str(document)
    assert "user-1" not in rendered
    assert "league-1" not in rendered
    assert "known" not in rendered


def test_build_home_marks_games_unavailable_when_one_team_fails(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sleeper = FakeSleeper()
    sleeper.manager_players = ["known", "bos"]
    sleeper.catalog["bos"] = {"full_name": "Bos Player", "team": "BOS", "espn_id": "2"}
    schedule = FakeSchedule()
    schedule.fail_teams.add("BOS")

    document = _build(sleeper, schedule, _repository(tmp_path)).payload

    assert document["games"] == {"status": "unavailable", "items": []}
    assert {row["name"] for row in document["roster"]} == {"Known Player", "Bos Player"}


def test_build_home_reuses_cached_catalog_and_schedule(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sleeper = FakeSleeper()
    schedule = FakeSchedule()
    repository = _repository(tmp_path)

    _build(sleeper, schedule, repository)
    cached = _build(sleeper, schedule, repository).payload

    assert sleeper.player_calls == 1
    assert schedule.calls == ["NOP"]
    assert cached["roster"][0]["positions"] == ["PG", "SG", "C", "G"]
    assert cached["roster"][0]["injury_status"] == "DTD"


def test_build_home_recommendation_failure_keeps_the_roster(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class Broken(AsyncSQLiteStateRepository):
        async def latest_current_recommendation(  # type: ignore[override]
            self,
            league_id: str,
            fantasy_week: int,
            *,
            decision_type: str,
        ) -> RecommendationRecord | None:
            raise RuntimeError("recommendation store down")

    repository = Broken(tmp_path / "state.db")
    asyncio.run(repository.initialize())
    document = _build(FakeSleeper(), FakeSchedule(), repository).payload

    assert document["recommendation"] == {"status": "unavailable"}
    assert document["roster"]


def test_build_home_catalog_failure_is_league_unavailable(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sleeper = FakeSleeper()
    sleeper.fail_players = True

    response = _build(sleeper, FakeSchedule(), _repository(tmp_path))

    assert response.status_code == 503
    assert response.payload == {"status": "league_unavailable"}


def test_build_home_bye_week_omits_opponent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sleeper = FakeSleeper()
    sleeper.matchup_payload = [{"roster_id": 1, "matchup_id": None, "points": 3}]

    document = _build(sleeper, FakeSchedule(), _repository(tmp_path)).payload

    assert document["matchup"]["opponent"] is None
    assert document["matchup"]["manager"]["score"] == 3.0


def test_build_home_week_zero_does_not_request_matchups(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sleeper = FakeSleeper()
    sleeper.state_payload = {"week": 0, "season": "2026", "season_type": "off"}

    document = _build(sleeper, FakeSchedule(), _repository(tmp_path)).payload

    assert sleeper.matchup_calls == 0
    assert document["matchup"]["opponent"] is None
    assert document["matchup"]["manager"]["score"] is None


def test_roster_rows_emits_empty_starter_before_bench() -> None:
    rows = roster_rows(["PG", "BN"], ["0"], ["bench"], [], {})
    assert rows is not None
    assert [(row.slot, row.name, row.nba_team, row.espn_id) for row in rows] == [
        ("PG", None, None, None),
        ("BN", None, None, None),
    ]


def test_roster_rows_rejects_starter_count_mismatch() -> None:
    assert roster_rows(["PG", "SG", "BN"], ["only"], ["only"], [], {}) is None


def test_build_home_repeats_a_player_with_two_games(tmp_path) -> None:  # type: ignore[no-untyped-def]
    schedule = FakeSchedule()
    schedule.starts = (TIPOFF, TIPOFF + timedelta(hours=3))

    items = _build(FakeSleeper(), schedule, _repository(tmp_path)).payload["games"]["items"]

    assert [item["player_name"] for item in items] == ["Known Player", "Known Player"]
    assert items[0]["start"] < items[1]["start"]


def test_build_home_copies_recommendation_text(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    asyncio.run(
        repository.create_recommendation(
            RecommendationRecord(
                recommendation_id="rec-1",
                idempotency_key="rec-1",
                league_id="league-1",
                fantasy_week=3,
                player_id="player-1",
                game_id=None,
                decision_type="weekly_lineup",
                title="Lineup move required",
                message="Move Known Player.",
                deadline=TIPOFF,
                policy_version="policy",
                created_at=NOW,
                trace_json='{"kind": "weekly_lineup", "status": "action_required"}',
            )
        )
    )

    recommendation = _build(FakeSleeper(), FakeSchedule(), repository).payload["recommendation"]

    assert recommendation["plan_status"] == "action_required"
    assert recommendation["title"] == "Lineup move required"
    assert recommendation["message"] == "Move Known Player."
    assert recommendation["deadline"] == TIPOFF.isoformat()
    assert "rec-1" not in str(recommendation)


def test_build_home_error_quality_makes_games_unavailable(tmp_path) -> None:  # type: ignore[no-untyped-def]
    schedule = FakeSchedule()
    schedule.error_teams.add("NOP")

    document = _build(FakeSleeper(), schedule, _repository(tmp_path)).payload

    assert document["games"] == {"status": "unavailable", "items": []}
    assert document["roster"][0]["name"] == "Known Player"


def test_build_home_resolves_missing_sleeper_espn_id(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sleeper = FakeSleeper()
    sleeper.catalog["known"] = {"full_name": "Known Player", "team": "NOP", "espn_id": None}
    schedule = FakeSchedule()
    schedule.roster_players = (
        ProviderPlayer(
            provider_id="3934672",
            full_name="Known Player",
            team_id="no",
            team_abbreviation="NO",
            active=True,
            source=SourceMetadata(provider="espn", provider_id="3934672", retrieved_at=NOW),
        ),
    )

    document = _build(sleeper, schedule, _repository(tmp_path)).payload

    assert document["roster"][0]["image_url"] == (
        "https://a.espncdn.com/i/headshots/nba/players/full/3934672.png"
    )
    assert schedule.roster_calls == ["NOP"]


def test_build_home_does_not_cache_unresolved_headshots(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sleeper = FakeSleeper()
    sleeper.catalog["known"] = {"full_name": "Known Player", "team": "NOP", "espn_id": None}
    schedule = FakeSchedule()
    schedule.roster_error = ESPNSchemaError("roster schema")
    repository = _repository(tmp_path)

    first = _build(sleeper, schedule, repository).payload
    second = _build(sleeper, schedule, repository).payload

    assert first["roster"][0]["image_url"] is None
    assert second["roster"][0]["name"] == "Known Player"
    assert schedule.roster_calls == ["NOP", "NOP"]


def test_build_home_schema_error_keeps_the_roster(tmp_path) -> None:  # type: ignore[no-untyped-def]
    schedule = FakeSchedule()
    schedule.schedule_error = ESPNSchemaError("schedule schema")

    response = _build(FakeSleeper(), schedule, _repository(tmp_path))

    assert response.status_code == 200
    assert response.payload["games"] == {"status": "unavailable", "items": []}
    assert response.payload["roster"][0]["name"] == "Known Player"


def test_build_home_keeps_manager_when_opponent_owner_is_missing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sleeper = FakeSleeper()
    sleeper.opponent_owner = None

    document = _build(sleeper, FakeSchedule(), _repository(tmp_path)).payload

    assert document["matchup"]["manager"]["score"] is None
    assert document["matchup"]["opponent"]["name"] is None
    assert document["matchup"]["opponent"]["score"] == 9.0
    assert document["roster"][0]["name"] == "Known Player"


def test_build_home_includes_season_records(tmp_path) -> None:  # type: ignore[no-untyped-def]
    matchup = _build(FakeSleeper(), FakeSchedule(), _repository(tmp_path)).payload["matchup"]

    assert matchup["manager"]["record"] == {"wins": 2, "losses": 1, "ties": 0}
    assert matchup["manager"]["points_for"] == 812.04
    assert matchup["opponent"]["record"] == {"wins": 1, "losses": 1, "ties": 1}
    assert matchup["opponent"]["points_for"] == 700.5


def test_build_home_missing_settings_leave_record_null(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sleeper = FakeSleeper()
    sleeper.manager_settings = None

    response = _build(sleeper, FakeSchedule(), _repository(tmp_path))

    assert response.status_code == 200
    assert response.payload["matchup"]["manager"]["record"] is None
    assert response.payload["matchup"]["manager"]["points_for"] is None


def test_roster_standings_reject_non_integer_settings() -> None:
    standings = roster_standings(
        [
            {"roster_id": 1, "settings": {"wins": True, "losses": 0, "ties": 0, "fpts": 10}},
            {"roster_id": 2, "settings": {"wins": 1, "losses": 0, "ties": 0, "fpts": "10"}},
            {"roster_id": "3", "settings": {"wins": 1, "losses": 0, "ties": 0}},
        ]
    )

    assert standings[1].record is None
    assert standings[1].points_for is None
    assert standings[2].record is not None
    assert standings[2].points_for is None
    assert 3 not in standings


def test_build_home_includes_positions_and_injury_status(tmp_path) -> None:  # type: ignore[no-untyped-def]
    document = _build(FakeSleeper(), FakeSchedule(), _repository(tmp_path)).payload

    assert document["roster"][0]["positions"] == ["PG", "SG", "C", "G"]
    assert document["roster"][0]["injury_status"] == "DTD"
    assert document["roster"][1]["positions"] == []
    assert document["roster"][1]["injury_status"] is None
    assert document["games"]["items"][0]["injury_status"] == "DTD"


def test_build_home_drops_blank_positions_and_non_string_injury(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sleeper = FakeSleeper()
    sleeper.catalog["known"] = {
        "full_name": "Known Player",
        "team": "NOP",
        "espn_id": "55",
        "fantasy_positions": [" ", "PG"],
        "injury_status": 1,
    }

    row = _build(sleeper, FakeSchedule(), _repository(tmp_path)).payload["roster"][0]

    assert row["name"] == "Known Player"
    assert row["positions"] == ["PG"]
    assert row["injury_status"] is None
