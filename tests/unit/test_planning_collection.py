from datetime import UTC, datetime, timedelta

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
    AvailabilityStatus,
    DataQualityReport,
    DataQualityState,
    GameStatus,
    PlayerAvailability,
    ProviderPlayer,
    ProviderResult,
    ScheduledGame,
    SourceMetadata,
)
from sleeper_manager.domain.planning import (
    AcknowledgedAction,
    AcknowledgedDecisionEvidence,
    PlanningReasonCode,
)
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.projections.live_baseline import LiveProjectionTarget
from sleeper_manager.workflows.planning_collection import (
    PlanningCollectionError,
    collect_live_planning_inputs,
)
from sleeper_manager.workflows.planning_inputs import (
    FantasyWeekWindow,
    LiveProjectionResult,
    PlanningFreshnessPolicy,
    build_live_team_week_state,
)

NOW = datetime(2026, 1, 7, 18, tzinfo=UTC)
WINDOW_START = datetime(2026, 1, 5, 5, tzinfo=UTC)
WINDOW_END = datetime(2026, 1, 12, 5, tzinfo=UTC)
RETRIEVED_AT = NOW - timedelta(minutes=5)


def _profile() -> LeagueProfile:
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
        roster_slots=(
            RosterSlot(index=0, position="PG", is_starting=True),
            RosterSlot(index=1, position="UTIL", is_starting=True),
            RosterSlot(index=2, position="BN", is_starting=False),
        ),
        scoring=ScoringPolicy(points=1, rebounds=1.2),
        users=(LeagueUser("user-1", None, None, None),),
        rosters=(
            Roster(
                roster_id=1,
                owner_id=None,
                player_ids=("p1", "p2"),
                starter_ids=("p1", "p2"),
            ),
        ),
        manager_user_id="user-1",
        manager_roster_id=1,
        fantasy_week=FantasyWeek(week=1, season="2026", season_type="regular"),
        transactions=(),
        configuration_fingerprint="config-fp-1",
        retrieved_at=RETRIEVED_AT,
    )


def _quality(
    state: DataQualityState = DataQualityState.FRESH,
    *,
    resource: str = "fixture",
) -> DataQualityReport:
    return DataQualityReport(
        state=state,
        resource=resource,
        record_count=0,
        retrieved_at=RETRIEVED_AT,
        source_updated_at=None,
        expires_at=None,
    )


def _game(
    game_id: str,
    *,
    start: datetime = NOW + timedelta(hours=2),
) -> ScheduledGame:
    return ScheduledGame(
        provider_id=game_id,
        start_time=start,
        status=GameStatus.SCHEDULED,
        home_team_id="12",
        away_team_id="14",
        status_detail=None,
        source=SourceMetadata(provider="espn", provider_id=game_id, retrieved_at=RETRIEVED_AT),
    )


class _StaticProfileSource:
    def __init__(self, profile: LeagueProfile) -> None:
        self.profile = profile

    async def fetch(self) -> LeagueProfile:
        return self.profile


class _StaticCatalog:
    def __init__(self, players: dict[str, dict[str, object]]) -> None:
        self.players_by_id = players

    async def players(self) -> dict[str, dict[str, object]]:
        return self.players_by_id


class _FakeNBA:
    def __init__(self) -> None:
        self.rosters: dict[str, tuple[ProviderPlayer, ...]] = {}
        self.schedules: dict[str, tuple[ScheduledGame, ...]] = {}
        self.injury_records: tuple[PlayerAvailability, ...] = ()
        self.schedule_failures: set[str] = set()
        self.injury_failure = False
        self.requested_schedule_teams: list[str] = []

    async def scoreboard(self, game_date):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def game_summary(self, game_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def team_roster(self, team_id: str) -> ProviderResult[tuple[ProviderPlayer, ...]]:
        return ProviderResult(self.rosters.get(team_id, ()), _quality(resource="team_roster"))

    async def team_schedule(
        self, team_id: str, season: int
    ) -> ProviderResult[tuple[ScheduledGame, ...]]:
        assert season == 2026
        self.requested_schedule_teams.append(team_id)
        if team_id in self.schedule_failures:
            raise RuntimeError("schedule endpoint down")
        return ProviderResult(self.schedules.get(team_id, ()), _quality())

    async def injuries(self) -> ProviderResult[tuple[PlayerAvailability, ...]]:
        if self.injury_failure:
            raise RuntimeError("injuries endpoint down")
        return ProviderResult(self.injury_records, _quality())


class _RecordingProjections:
    def __init__(self, expected_value: float = 10) -> None:
        self.targets: list[LiveProjectionTarget] = []
        self.expected_value = expected_value

    def project(
        self,
        target: LiveProjectionTarget,
        *,
        scoring_policy: ScoringPolicy,
        decision_time: datetime,
    ) -> object:
        from sleeper_manager.domain.planning import ProjectionSnapshot
        from sleeper_manager.domain.projection import ProjectionDistribution

        self.targets.append(target)
        distribution = ProjectionDistribution(
            expected_value=self.expected_value,
            median=self.expected_value,
            percentiles=((50, self.expected_value),),
            lower_bound=self.expected_value,
            upper_bound=self.expected_value,
            variance=0,
        )
        return ProjectionSnapshot(
            player_id=target.sleeper_player_id,
            game_id=target.game_id,
            available_as_of=decision_time,
            model_version="baseline-v1",
            input_version="live-inputs-v1",
            scoring_policy_version=scoring_policy.version,
            distribution=distribution,
            reasons=(),
        )


def _catalog() -> dict[str, dict[str, object]]:
    return {
        "p1": {
            "full_name": "Point Guard One",
            "team": "CHI",
            "espn_id": 401,
            "fantasy_positions": ["PG", "SG"],
        },
        "p2": {
            "full_name": "Center Two",
            "team": "CHI",
            "espn_id": 402,
            "fantasy_positions": ["C"],
        },
    }


def _nba() -> _FakeNBA:
    nba = _FakeNBA()
    nba.rosters["CHI"] = (
        ProviderPlayer("401", "Point Guard One", "12", "chi", True, _provider_source()),
        ProviderPlayer("402", "Center Two", "12", "chi", True, _provider_source()),
    )
    nba.schedules["12"] = (_game("g1"), _game("g2"))
    nba.injury_records = (
        PlayerAvailability(
            player_id="401",
            status=AvailabilityStatus.QUESTIONABLE,
            detail="ankle",
            source=_provider_source(),
        ),
    )
    return nba


def _provider_source() -> SourceMetadata:
    return SourceMetadata(provider="espn", provider_id="x", retrieved_at=RETRIEVED_AT)


def _freshness_policy() -> PlanningFreshnessPolicy:
    return PlanningFreshnessPolicy(
        max_sleeper_age=timedelta(hours=6),
        max_nba_schedule_age=timedelta(hours=6),
        max_availability_age=timedelta(hours=3),
    )


async def _collect(nba: _FakeNBA, projections: _RecordingProjections, **overrides) -> object:
    kwargs = {
        "profile_source": _StaticProfileSource(_profile()),
        "catalog_source": _StaticCatalog(_catalog()),
        "nba": nba,
        "projection_provider": projections,
        "week_window": FantasyWeekWindow(1, WINDOW_START, WINDOW_END),
        "freshness_policy": _freshness_policy(),
        "clock": lambda: NOW,
    }
    kwargs.update(overrides)
    return await collect_live_planning_inputs(**kwargs)


def test_collect_and_assemble_complete_live_state() -> None:
    projections = _RecordingProjections()

    async def run() -> None:
        evidence = await _collect(_nba(), projections)
        state = build_live_team_week_state(evidence.inputs, decision_time=evidence.decision_time)
        return evidence, state

    evidence, state = asyncio_run(run())

    assert evidence.inputs.week_window.week == 1
    assert not state.is_blocked
    assert len(state.opportunities) == 4
    assert all(opportunity.projection is not None for opportunity in state.opportunities)
    first = next(opportunity for opportunity in state.opportunities if opportunity.game_id == "g1")
    assert first.availability_status == "questionable"
    assert first.eligible_slot_indices == (0, 1)
    resources = {source.source for source in state.freshness.sources}
    assert "nba:team-schedule:12" in resources and "nba:injuries" in resources
    assert [target.game_id for target in projections.targets] == ["g1", "g2", "g1", "g2"]
    assert [target.sleeper_player_id for target in projections.targets][:2] == ["p1", "p1"]


def test_roster_freshness_sources_are_scoped_per_team() -> None:
    catalog = {
        "p1": {
            "full_name": "Point Guard One",
            "team": "CHI",
            "espn_id": 401,
            "fantasy_positions": ["PG"],
        },
        "p2": {
            "full_name": "Center Two",
            "team": "BOS",
            "espn_id": 402,
            "fantasy_positions": ["C"],
        },
    }
    nba = _FakeNBA()
    nba.rosters["CHI"] = (
        ProviderPlayer("401", "Point Guard One", "12", "chi", True, _provider_source()),
    )
    nba.rosters["BOS"] = (
        ProviderPlayer("402", "Center Two", "14", "bos", True, _provider_source()),
    )
    projections = _RecordingProjections()

    async def run() -> object:
        return await _collect(
            nba,
            projections,
            catalog_source=_StaticCatalog(catalog),
        )

    evidence = asyncio_run(run())
    roster_resources = [report.resource for report in evidence.inputs.identity_quality_reports]

    assert roster_resources == ["nba:team-roster:BOS", "nba:team-roster:CHI"]
    state = build_live_team_week_state(evidence.inputs, decision_time=evidence.decision_time)
    assert {
        source.source
        for source in state.freshness.sources
        if source.source.startswith("nba:team-roster:")
    } == set(roster_resources)


def test_schedule_provider_failure_blocks_planning() -> None:
    nba = _nba()
    nba.schedule_failures.add("12")

    async def run() -> None:
        return await _collect(nba, _RecordingProjections())

    evidence = asyncio_run(run())

    assert any("team-schedule:12" in warning for warning in evidence.warnings)
    state = build_live_team_week_state(evidence.inputs, decision_time=evidence.decision_time)
    assert PlanningReasonCode.STALE_NBA_STATE in state.blocking_reasons
    assert state.opportunities == ()


def test_injury_provider_failure_blocks_planning() -> None:
    nba = _nba()
    nba.injury_failure = True

    async def run() -> None:
        return await _collect(nba, _RecordingProjections())

    evidence = asyncio_run(run())
    state = build_live_team_week_state(evidence.inputs, decision_time=evidence.decision_time)
    assert PlanningReasonCode.STALE_NBA_STATE in state.blocking_reasons


def test_missing_catalog_player_stays_unresolved() -> None:
    catalog = _catalog()
    del catalog["p2"]

    async def run() -> None:
        return await _collect(
            _nba(), _RecordingProjections(), catalog_source=_StaticCatalog(catalog)
        )

    evidence = asyncio_run(run())

    assert any("p2" in warning for warning in evidence.warnings)
    state = build_live_team_week_state(evidence.inputs, decision_time=evidence.decision_time)
    assert PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY in state.blocking_reasons


def test_out_of_window_games_are_not_projected() -> None:
    nba = _nba()
    nba.schedules["12"] = (
        _game("g1"),
        _game("g9", start=WINDOW_END + timedelta(days=1)),
    )
    projections = _RecordingProjections()

    async def run() -> None:
        return await _collect(nba, projections)

    evidence = asyncio_run(run())

    assert {target.game_id for target in projections.targets} == {"g1"}
    state = build_live_team_week_state(evidence.inputs, decision_time=evidence.decision_time)
    assert {opportunity.game_id for opportunity in state.opportunities} == {"g1"}


def test_mapping_override_resolves_identity() -> None:
    catalog = {
        "p1": {"full_name": "Renamed Guard", "team": "CHI", "fantasy_positions": ["PG"]},
        "p2": {
            "full_name": "Center Two",
            "team": "CHI",
            "espn_id": 402,
            "fantasy_positions": ["C"],
        },
    }

    async def run() -> None:
        return await _collect(
            _nba(),
            _RecordingProjections(),
            catalog_source=_StaticCatalog(catalog),
            mapping_overrides={"p1": "401"},
        )

    evidence = asyncio_run(run())

    identities = {item.sleeper_player_id: item for item in evidence.inputs.identities}
    assert identities["p1"].provider_player_id == "401"
    assert identities["p1"].method == "explicit_override"


def test_naive_clock_output_is_rejected() -> None:
    async def run() -> None:
        await _collect(_nba(), _RecordingProjections(), clock=lambda: datetime(2026, 1, 7, 18))

    with pytest.raises(PlanningCollectionError, match="timezone-aware"):
        asyncio_run(run())


def test_eligibility_is_stamped_with_catalog_retrieval_time() -> None:
    ticks = iter([NOW - timedelta(hours=1), NOW, NOW, NOW, NOW])

    async def run() -> None:
        return await _collect(_nba(), _RecordingProjections(), clock=lambda: next(ticks))

    evidence = asyncio_run(run())
    eligibility = {item.sleeper_player_id: item for item in evidence.inputs.player_eligibility}
    assert eligibility["p1"].available_as_of == NOW - timedelta(hours=1)
    assert evidence.decision_time == NOW
    state = build_live_team_week_state(evidence.inputs, decision_time=evidence.decision_time)
    assert not state.is_blocked


class _StaticAcknowledgements:
    def __init__(self, records: tuple[AcknowledgedDecisionEvidence, ...]) -> None:
        self.records = records
        self.requested: tuple[str, int] | None = None

    async def load(self, league_id: str, week: int, *, as_of: datetime):
        self.requested = (league_id, week)
        return self.records


def test_acknowledgement_source_feeds_the_bundle() -> None:
    passed = AcknowledgedDecisionEvidence(
        decision_id="rec-pass-9",
        player_id="p1",
        game_id="g1",
        action=AcknowledgedAction.PASS,
        decided_at=NOW - timedelta(hours=2),
        provenance="repository-query",
    )
    source = _StaticAcknowledgements((passed,))

    async def run() -> None:
        return await _collect(_nba(), _RecordingProjections(), acknowledgement_source=source)

    evidence = asyncio_run(run())
    assert source.requested == ("league-1", 1)
    assert len(evidence.inputs.acknowledgements) == 1
    state = build_live_team_week_state(evidence.inputs, decision_time=evidence.decision_time)
    assert ("p1", "g1") not in {
        (item.sleeper_player_id, item.game_id) for item in state.unpassed_opportunities
    }


def asyncio_run(awaitable):  # noqa: ANN001
    import asyncio

    return asyncio.run(awaitable)


def test_result_type_shape_is_preserved() -> None:
    result = LiveProjectionResult(
        sleeper_player_id="p1",
        game_id="g1",
        snapshot=None,
        missing_reason=PlanningReasonCode.MISSING_PROJECTION,
    )
    assert result.snapshot is None
