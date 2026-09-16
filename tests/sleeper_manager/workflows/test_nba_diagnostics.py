from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any

import pytest

from sleeper_manager.domain.league import (
    FantasyWeek,
    LeagueMode,
    LeagueProfile,
    LeagueUser,
    RosterSlot,
)
from sleeper_manager.domain.models import Roster
from sleeper_manager.domain.nba import (
    DataQualityReport,
    DataQualityState,
    ProviderPlayer,
    ProviderResult,
    SourceMetadata,
)
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.nba.identity import MappingMethod
from sleeper_manager.integrations.sleeper.sync import LeagueSyncResult
from sleeper_manager.workflows.nba_diagnostics import NBAHealthReport, collect_nba_diagnostics

NOW = datetime(2026, 1, 7, 18, tzinfo=UTC)
GAME_DATE = date(2026, 1, 7)
SOURCE = SourceMetadata("espn", "test", NOW)


def _profile(*, player_ids: tuple[str, ...] = ("p1",)) -> LeagueProfile:
    return LeagueProfile(
        league_id="league-1",
        name="Fixture League",
        sport="nba",
        season="2026",
        season_type="regular",
        status="in_season",
        total_rosters=1,
        previous_league_id=None,
        mode=LeagueMode.LOCK_IN,
        roster_slots=(RosterSlot(index=0, position="PG", is_starting=True),),
        scoring=ScoringPolicy(points=1),
        users=(LeagueUser("user-1", None, None, None),),
        rosters=(
            Roster(
                roster_id=1,
                owner_id="user-1",
                player_ids=player_ids,
                starter_ids=player_ids,
            ),
        ),
        manager_user_id="user-1",
        manager_roster_id=1,
        fantasy_week=FantasyWeek(week=1, season="2026", season_type="regular"),
        transactions=(),
        configuration_fingerprint="fp-1",
        retrieved_at=NOW,
    )


def _empty_scoreboard() -> ProviderResult[tuple[object, ...]]:
    return ProviderResult((), _quality(DataQualityState.EMPTY, resource="scoreboard"))


def _quality(
    state: DataQualityState = DataQualityState.FRESH,
    *,
    resource: str = "team_roster",
) -> DataQualityReport:
    return DataQualityReport(
        state=state,
        resource=resource,
        record_count=1,
        retrieved_at=NOW,
        source_updated_at=None,
        expires_at=None,
    )


class _FakeSleeper:
    def __init__(
        self,
        catalog: dict[str, dict[str, object]] | None = None,
        *,
        catalog_error: Exception | None = None,
    ) -> None:
        self.catalog = catalog or {}
        self.catalog_error = catalog_error

    async def players(self, *, active: bool = True) -> dict[str, dict[str, object]]:
        del active
        if self.catalog_error is not None:
            raise self.catalog_error
        return self.catalog


class _FakeNBA:
    def __init__(self) -> None:
        self.rosters: dict[str, ProviderResult[tuple[ProviderPlayer, ...]]] = {}
        self.roster_errors: dict[str, Exception] = {}
        self.scoreboard_result: ProviderResult[tuple[object, ...]] | None = None
        self.scoreboard_error: Exception | None = None
        self.requested_rosters: list[str] = []

    async def team_roster(self, team_id: str) -> ProviderResult[tuple[ProviderPlayer, ...]]:
        self.requested_rosters.append(team_id)
        if team_id in self.roster_errors:
            raise self.roster_errors[team_id]
        return self.rosters[team_id]

    async def scoreboard(self, game_date: date) -> ProviderResult[tuple[object, ...]]:
        del game_date
        if self.scoreboard_error is not None:
            raise self.scoreboard_error
        assert self.scoreboard_result is not None
        return self.scoreboard_result


def _patch_sync(monkeypatch: pytest.MonkeyPatch, result: LeagueSyncResult | Exception) -> None:
    class _Service:
        def __init__(self, sleeper: object) -> None:
            del sleeper

        async def sync(self, *, league_id: str, user_id: str) -> LeagueSyncResult:
            del league_id, user_id
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(
        "sleeper_manager.workflows.nba_diagnostics.LeagueSynchronizationService",
        _Service,
    )


def _run(
    sleeper: _FakeSleeper,
    nba: _FakeNBA,
    **overrides: Any,
) -> NBAHealthReport:
    kwargs: dict[str, Any] = {
        "league_id": "league-1",
        "user_id": "user-1",
        "game_date": GAME_DATE,
    }
    kwargs.update(overrides)
    return asyncio.run(collect_nba_diagnostics(sleeper, nba, **kwargs))  # type: ignore[arg-type]


def test_healthy_when_mapping_resolves_and_quality_is_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sync(
        monkeypatch,
        LeagueSyncResult(_profile(), configuration_changed=False, previous_fingerprint=None),
    )
    nba = _FakeNBA()
    nba.rosters["CHI"] = ProviderResult(
        (ProviderPlayer("401", "Guard One", "CHI", "chi", True, SOURCE),),
        _quality(),
    )
    nba.scoreboard_result = _empty_scoreboard()
    sleeper = _FakeSleeper(
        {"p1": {"full_name": "Guard One", "team": "CHI", "espn_id": "401"}},
    )

    report = _run(sleeper, nba)

    assert report.healthy
    assert report.mapping.mappings[0].method is MappingMethod.STABLE_ID
    assert nba.requested_rosters == ["CHI"]


def test_bootstrap_failure_is_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sync(monkeypatch, RuntimeError("league missing"))
    report = _run(_FakeSleeper(), _FakeNBA())

    assert not report.healthy
    assert report.errors == ("Sleeper bootstrap failed: league missing",)
    assert report.quality_reports == ()


def test_missing_and_invalid_catalog_players_become_mapping_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sync(
        monkeypatch,
        LeagueSyncResult(
            _profile(player_ids=("missing", "bad")),
            configuration_changed=False,
            previous_fingerprint=None,
        ),
    )
    nba = _FakeNBA()
    nba.scoreboard_result = _empty_scoreboard()
    sleeper = _FakeSleeper({"bad": {"full_name": "", "team": "CHI"}})

    report = _run(sleeper, nba)

    assert not report.healthy
    assert any("missing from the catalog" in warning for warning in report.mapping.warnings)
    assert any("has no name" in warning for warning in report.mapping.warnings)
    assert nba.requested_rosters == []


def test_catalog_failure_still_attempts_scoreboard(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sync(
        monkeypatch,
        LeagueSyncResult(_profile(), configuration_changed=False, previous_fingerprint=None),
    )
    nba = _FakeNBA()
    nba.scoreboard_result = _empty_scoreboard()
    sleeper = _FakeSleeper(catalog_error=RuntimeError("catalog down"))

    report = _run(sleeper, nba)

    assert report.errors == ("Sleeper player catalog failed: catalog down",)
    assert report.quality_reports[0].resource == "scoreboard"


def test_roster_and_scoreboard_failures_are_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sync(
        monkeypatch,
        LeagueSyncResult(_profile(), configuration_changed=False, previous_fingerprint=None),
    )
    nba = _FakeNBA()
    nba.roster_errors["CHI"] = RuntimeError("roster down")
    nba.scoreboard_error = RuntimeError("scoreboard down")
    sleeper = _FakeSleeper({"p1": {"full_name": "Guard One", "team": "CHI", "espn_id": "401"}})

    report = _run(sleeper, nba)

    assert not report.healthy
    assert report.errors == (
        "ESPN team roster CHI failed: roster down",
        "ESPN scoreboard failed: scoreboard down",
    )


def test_stale_quality_is_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sync(
        monkeypatch,
        LeagueSyncResult(_profile(), configuration_changed=False, previous_fingerprint=None),
    )
    nba = _FakeNBA()
    nba.rosters["CHI"] = ProviderResult(
        (ProviderPlayer("401", "Guard One", "CHI", "chi", True, SOURCE),),
        _quality(DataQualityState.STALE),
    )
    nba.scoreboard_result = _empty_scoreboard()
    sleeper = _FakeSleeper({"p1": {"full_name": "Guard One", "team": "CHI", "espn_id": "401"}})

    assert not _run(sleeper, nba).healthy


def test_mapping_override_resolves_without_espn_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sync(
        monkeypatch,
        LeagueSyncResult(_profile(), configuration_changed=False, previous_fingerprint=None),
    )
    nba = _FakeNBA()
    nba.rosters["CHI"] = ProviderResult(
        (ProviderPlayer("401", "Guard One", "CHI", "chi", True, SOURCE),),
        _quality(),
    )
    nba.scoreboard_result = _empty_scoreboard()
    sleeper = _FakeSleeper({"p1": {"full_name": "Guard One", "team": "CHI"}})

    report = _run(sleeper, nba, mapping_overrides={"p1": "401"})

    assert report.healthy
    assert report.mapping.mappings[0].method is MappingMethod.EXPLICIT_OVERRIDE
