"""Shared live-planning input factories used across workflow tests."""

from datetime import UTC, datetime, timedelta

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
    ScheduledGame,
    SourceMetadata,
)
from sleeper_manager.domain.planning import (
    AcknowledgedDecisionEvidence,
    ProjectionSnapshot,
)
from sleeper_manager.domain.projection import ProjectionDistribution
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.workflows.planning_inputs import (
    AvailabilityResourceResult,
    FantasyWeekWindow,
    LivePlanningInputs,
    LiveProjectionResult,
    PlanningFreshnessPolicy,
    PlayerEligibilityEvidence,
    ResolvedPlayerIdentity,
    ScheduleResourceResult,
)

NOW = datetime(2026, 1, 7, 18, tzinfo=UTC)
WINDOW_START = datetime(2026, 1, 5, 5, tzinfo=UTC)
WINDOW_END = datetime(2026, 1, 12, 5, tzinfo=UTC)
RETRIEVED_AT = NOW - timedelta(minutes=5)


def _distribution(expected: float = 10) -> ProjectionDistribution:
    return ProjectionDistribution(
        expected_value=expected,
        median=expected,
        percentiles=((50, expected),),
        lower_bound=expected,
        upper_bound=expected,
        variance=0,
    )


def _snapshot(
    player_id: str = "p1",
    game_id: str = "g1",
    *,
    available_as_of: datetime = RETRIEVED_AT,
) -> ProjectionSnapshot:
    return ProjectionSnapshot(
        player_id=player_id,
        game_id=game_id,
        available_as_of=available_as_of,
        model_version="baseline-v1",
        input_version="live-inputs-v1",
        scoring_policy_version="scoring-v1",
        distribution=_distribution(),
        reasons=(),
    )


def _source(retrieved_at: datetime = RETRIEVED_AT) -> SourceMetadata:
    return SourceMetadata(provider="espn", provider_id="x", retrieved_at=retrieved_at)


def _game(
    game_id: str = "g1",
    *,
    start: datetime = NOW + timedelta(hours=2),
    status: GameStatus = GameStatus.SCHEDULED,
    home_team_id: str = "12",
    away_team_id: str = "14",
) -> ScheduledGame:
    return ScheduledGame(
        provider_id=game_id,
        start_time=start,
        status=status,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        status_detail=None,
        source=_source(),
    )


def _quality(
    state: DataQualityState = DataQualityState.FRESH,
    *,
    resource: str = "espn:team-schedule:12",
    retrieved_at: datetime = RETRIEVED_AT,
) -> DataQualityReport:
    return DataQualityReport(
        state=state,
        resource=resource,
        record_count=1,
        retrieved_at=retrieved_at,
        source_updated_at=None,
        expires_at=None,
    )


def _profile(
    *,
    starter_ids: tuple[str | None, ...] = ("p1", "p2"),
    player_ids: tuple[str, ...] = ("p1", "p2"),
    retrieved_at: datetime = RETRIEVED_AT,
    roster_ids: tuple[int, ...] = (1,),
) -> LeagueProfile:
    slots = (
        RosterSlot(index=0, position="PG", is_starting=True),
        RosterSlot(index=1, position="UTIL", is_starting=True),
        RosterSlot(index=2, position="BN", is_starting=False),
    )
    rosters = tuple(
        Roster(
            roster_id=roster_id,
            owner_id=None,
            player_ids=player_ids if roster_id == 1 else (),
            starter_ids=starter_ids if roster_id == 1 else (),
        )
        for roster_id in roster_ids
    )
    return LeagueProfile(
        league_id="league-1",
        name="Fixture League",
        sport="nba",
        season="2026",
        season_type="regular",
        status="in_season",
        total_rosters=len(rosters),
        previous_league_id=None,
        mode=LeagueMode.LOCK_IN,
        roster_slots=slots,
        scoring=ScoringPolicy(points=1, rebounds=1.2),
        users=(LeagueUser("user-1", None, None, None),),
        rosters=rosters,
        manager_user_id="user-1",
        manager_roster_id=1,
        fantasy_week=FantasyWeek(week=1, season="2026", season_type="regular"),
        transactions=(),
        configuration_fingerprint="config-fp-1",
        retrieved_at=retrieved_at,
    )


def _eligibility(
    player_id: str = "p1",
    positions: tuple[str, ...] = ("PG", "SG"),
) -> PlayerEligibilityEvidence:
    return PlayerEligibilityEvidence(
        sleeper_player_id=player_id,
        eligible_positions=positions,
        available_as_of=RETRIEVED_AT,
        provenance="sleeper-players",
    )


def _identity(
    player_id: str = "p1",
    *,
    provider_player_id: str | None = "401",
    provider_team_id: str | None = "12",
) -> ResolvedPlayerIdentity:
    return ResolvedPlayerIdentity(
        sleeper_player_id=player_id,
        provider_player_id=provider_player_id,
        provider_team_id=provider_team_id,
        method="explicit_override",
        confidence="high",
        reason="manager override",
    )


def _schedule_result(*games: ScheduledGame, **kwargs) -> ScheduleResourceResult:
    return ScheduleResourceResult(
        resource=kwargs.get("resource", "espn:team-schedule:12"),
        games=tuple(games),
        quality=kwargs.get("quality", _quality()),
    )


def _availability_result(
    *records: PlayerAvailability,
    state: DataQualityState = DataQualityState.FRESH,
) -> AvailabilityResourceResult:
    return AvailabilityResourceResult(
        resource="espn:injuries",
        records=records,
        quality=_quality(state, resource="espn:injuries"),
    )


def _availability(
    player_id: str = "401",
    status: AvailabilityStatus = AvailabilityStatus.QUESTIONABLE,
) -> PlayerAvailability:
    return PlayerAvailability(
        player_id=player_id,
        status=status,
        detail="ankle",
        source=_source(),
    )


def _inputs(
    profile: LeagueProfile | None = None,
    *,
    schedule_results: tuple[ScheduleResourceResult, ...] | None = None,
    projections: tuple[LiveProjectionResult, ...] = (),
    acknowledgements: tuple[AcknowledgedDecisionEvidence, ...] = (),
    freshness_policy: PlanningFreshnessPolicy | None = None,
    runtime_policy_version: str = "runtime-policy-v7",
) -> LivePlanningInputs:
    return LivePlanningInputs(
        league_profile=profile or _profile(),
        week_window=FantasyWeekWindow(1, WINDOW_START, WINDOW_END),
        freshness_policy=freshness_policy or _freshness_policy(),
        runtime_policy_version=runtime_policy_version,
        move_lead_time=timedelta(minutes=10),
        player_eligibility=(_eligibility("p1"), _eligibility("p2", ("C",))),
        identities=(
            _identity("p1"),
            _identity("p2", provider_player_id="402", provider_team_id="12"),
        ),
        schedule_results=schedule_results
        if schedule_results is not None
        else (_schedule_result(_game(), _game("g2")),),
        availability_results=(_availability_result(_availability()),),
        projections=projections,
        acknowledgements=acknowledgements,
    )


def _freshness_policy(
    *,
    max_nba_schedule_age: timedelta = timedelta(hours=6),
    max_availability_age: timedelta = timedelta(hours=3),
    max_sleeper_age: timedelta = timedelta(hours=6),
) -> PlanningFreshnessPolicy:
    return PlanningFreshnessPolicy(
        max_sleeper_age=max_sleeper_age,
        max_nba_schedule_age=max_nba_schedule_age,
        max_availability_age=max_availability_age,
    )
