import asyncio
import json
from datetime import UTC, datetime, timedelta

from test_notification_loop import RecordingSender

from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
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
from sleeper_manager.domain.planning import ProjectionSnapshot
from sleeper_manager.domain.projection import ProjectionDistribution
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.notifications.dispatcher import NotificationDispatcher
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import RecommendationStatus
from sleeper_manager.workflows.daily_plan import run_daily_plan
from sleeper_manager.workflows.notification_loop import NotificationLoop
from sleeper_manager.workflows.plan_rendering import WEEKLY_LINEUP_DECISION_TYPE
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
GAME_START = NOW + timedelta(hours=2)


def _distribution(expected: float) -> ProjectionDistribution:
    return ProjectionDistribution(
        expected_value=expected,
        median=expected,
        percentiles=((50, expected),),
        lower_bound=expected,
        upper_bound=expected,
        variance=0,
    )


def _snapshot(
    player_id: str,
    game_id: str,
    *,
    expected: float,
) -> ProjectionSnapshot:
    return ProjectionSnapshot(
        player_id=player_id,
        game_id=game_id,
        available_as_of=RETRIEVED_AT,
        model_version="baseline-v1",
        input_version="live-inputs-v1",
        scoring_policy_version="scoring-v1",
        distribution=_distribution(expected),
        reasons=(),
    )


def _source(retrieved_at: datetime = RETRIEVED_AT) -> SourceMetadata:
    return SourceMetadata(provider="espn", provider_id="x", retrieved_at=retrieved_at)


def _game(game_id: str = "g1") -> ScheduledGame:
    return ScheduledGame(
        provider_id=game_id,
        start_time=GAME_START,
        status=GameStatus.SCHEDULED,
        home_team_id="12",
        away_team_id="14",
        status_detail=None,
        source=_source(),
    )


def _quality(
    state: DataQualityState = DataQualityState.FRESH,
    *,
    resource: str = "espn:team-schedule:12",
) -> DataQualityReport:
    return DataQualityReport(
        state=state,
        resource=resource,
        record_count=1,
        retrieved_at=RETRIEVED_AT,
        source_updated_at=None,
        expires_at=None,
    )


def _profile(
    *,
    starter_ids: tuple[str | None, ...] = ("p1", "p2"),
    retrieved_at: datetime = RETRIEVED_AT,
) -> LeagueProfile:
    slots = (
        RosterSlot(index=0, position="PG", is_starting=True),
        RosterSlot(index=1, position="UTIL", is_starting=True),
        RosterSlot(index=2, position="BN", is_starting=False),
    )
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
        roster_slots=slots,
        scoring=ScoringPolicy(points=1, rebounds=1.2),
        users=(LeagueUser("user-1", None, None, None),),
        rosters=(
            Roster(
                roster_id=1,
                owner_id=None,
                player_ids=("p1", "p2"),
                starter_ids=starter_ids,
            ),
        ),
        manager_user_id="user-1",
        manager_roster_id=1,
        fantasy_week=FantasyWeek(week=1, season="2026", season_type="regular"),
        transactions=(),
        configuration_fingerprint="config-fp-1",
        retrieved_at=retrieved_at,
    )


def _inputs(
    profile: LeagueProfile | None = None,
    *,
    projections: tuple[LiveProjectionResult, ...],
    runtime_policy_version: str = "runtime-policy-v7",
    move_lead_time: timedelta = timedelta(minutes=10),
) -> LivePlanningInputs:
    return LivePlanningInputs(
        league_profile=profile or _profile(),
        week_window=FantasyWeekWindow(1, WINDOW_START, WINDOW_END),
        freshness_policy=PlanningFreshnessPolicy(
            max_sleeper_age=timedelta(hours=6),
            max_nba_schedule_age=timedelta(hours=6),
            max_availability_age=timedelta(hours=3),
        ),
        runtime_policy_version=runtime_policy_version,
        move_lead_time=move_lead_time,
        player_eligibility=(
            PlayerEligibilityEvidence("p1", ("PG", "SG"), RETRIEVED_AT, "sleeper-players"),
            PlayerEligibilityEvidence("p2", ("C", "UTIL"), RETRIEVED_AT, "sleeper-players"),
        ),
        identities=(
            ResolvedPlayerIdentity("p1", "401", "12", "override", "high", "override"),
            ResolvedPlayerIdentity("p2", "402", "12", "override", "high", "override"),
        ),
        schedule_results=(ScheduleResourceResult("espn:team-schedule:12", (_game(),), _quality()),),
        availability_results=(
            AvailabilityResourceResult(
                "espn:injuries",
                (
                    PlayerAvailability(
                        "401",
                        AvailabilityStatus.AVAILABLE,
                        "",
                        _source(),
                    ),
                    PlayerAvailability(
                        "402",
                        AvailabilityStatus.AVAILABLE,
                        "",
                        _source(),
                    ),
                ),
                _quality(resource="espn:injuries"),
            ),
        ),
        projections=projections,
    )


def _workflow(tmp_path, *, clock=lambda: NOW):
    repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
    sender = RecordingSender()
    notifications = NotificationLoop(
        repository,
        NotificationDispatcher(sender),
        acknowledgement_base_url="https://example.test/ack",
        clock=clock,
    )
    return repository, sender, notifications


def _projections(
    *,
    p1_expected: float = 30.0,
    p2_expected: float = 10.0,
) -> tuple[LiveProjectionResult, ...]:
    return (
        LiveProjectionResult("p1", "g1", _snapshot("p1", "g1", expected=p1_expected), None),
        LiveProjectionResult("p2", "g1", _snapshot("p2", "g1", expected=p2_expected), None),
    )


async def _run_async(
    tmp_path,
    inputs: LivePlanningInputs,
    *,
    decision_time: datetime = NOW,
    clock=lambda: NOW,
    repository: AsyncSQLiteStateRepository | None = None,
    notifications: NotificationLoop | None = None,
    sender: RecordingSender | None = None,
):
    if repository is None or notifications is None or sender is None:
        repository, sender, notifications = _workflow(tmp_path, clock=clock)
        await repository.initialize()
    result = await run_daily_plan(
        inputs,
        decision_time=decision_time,
        repository=repository,
        notifications=notifications,
        open_sleeper_url="https://sleeper.com/league",
        player_names={"p1": "Ann", "p2": "Ben"},
        policy=WeeklyPlanPolicyConfig(scenario_count=8),
        clock=clock,
    )
    return result, sender, repository, notifications


def test_morning_no_action_plan_sends_nothing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        inputs = _inputs(projections=_projections(p1_expected=30.0, p2_expected=10.0))
        result, sender, repository, _ = await _run_async(tmp_path, inputs)

        assert result.outcome == "no_action"
        assert len(sender.messages) == 0
        assert result.recommendation is not None
        trace = json.loads(result.recommendation.trace_json)
        assert trace["trigger"] == "daily"
        assert trace["status"] == "no_action"
        pending = await repository.list_pending_recommendations(
            "league-1",
            1,
            decision_type=WEEKLY_LINEUP_DECISION_TYPE,
        )
        assert len(pending) == 1

    asyncio.run(exercise())


def test_one_required_move_sends_one_notification(tmp_path) -> None:  # type: ignore[no-untyped-def]
    profile = _profile(starter_ids=("p1", None))
    inputs = _inputs(profile, projections=_projections(p1_expected=10.0, p2_expected=30.0))
    result, sender, _, _ = asyncio.run(_run_async(tmp_path, inputs))

    assert result.outcome == "notified"
    assert len(sender.messages) == 1
    assert result.recommendation is not None
    assert result.recommendation.player_id == "p2"
    assert result.recommendation.game_id == "g1"
    assert result.notification is not None
    assert [action.label for action in result.notification.actions] == ["Open Sleeper"]
    assert "Start Ben in UTIL." in sender.messages[0].message
    assert result.plan.manager_policy_version == "runtime-policy-v7"
    assert result.plan.moves[0].deadline == GAME_START - timedelta(minutes=10)
    trace = json.loads(result.recommendation.trace_json)
    assert trace["manager_policy_version"] == "runtime-policy-v7"


def test_runtime_policy_controls_move_lead_and_material_identity(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "standard").mkdir()
    (tmp_path / "conservative").mkdir()
    profile = _profile(starter_ids=("p1", None))
    standard = _inputs(
        profile,
        projections=_projections(p1_expected=10.0, p2_expected=30.0),
    )
    conservative = _inputs(
        profile,
        projections=_projections(p1_expected=10.0, p2_expected=30.0),
        runtime_policy_version="runtime-policy-v8",
        move_lead_time=timedelta(minutes=30),
    )

    standard_result, _, _, _ = asyncio.run(_run_async(tmp_path / "standard", standard))
    conservative_result, _, _, _ = asyncio.run(_run_async(tmp_path / "conservative", conservative))

    assert conservative_result.plan.moves[0].deadline == GAME_START - timedelta(minutes=30)
    assert standard_result.plan.material_hash != conservative_result.plan.material_hash


def test_scheduler_retry_sends_no_duplicate(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        profile = _profile(starter_ids=("p1", None))
        inputs = _inputs(profile, projections=_projections(p1_expected=10.0, p2_expected=30.0))
        repository, sender, notifications = _workflow(tmp_path)
        await repository.initialize()
        first, _, _, _ = await _run_async(
            tmp_path,
            inputs,
            repository=repository,
            notifications=notifications,
            sender=sender,
        )
        second, _, _, _ = await _run_async(
            tmp_path,
            inputs,
            repository=repository,
            notifications=notifications,
            sender=sender,
        )

        assert first.outcome == "notified"
        assert second.outcome == "duplicate"
        assert len(sender.messages) == 1

    asyncio.run(exercise())


def test_explanation_only_drift_sends_no_update(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        profile = _profile(starter_ids=("p1", None))
        first_inputs = _inputs(
            profile, projections=_projections(p1_expected=10.0, p2_expected=30.0)
        )
        drifted_inputs = _inputs(
            profile, projections=_projections(p1_expected=10.0, p2_expected=35.0)
        )
        repository, sender, notifications = _workflow(tmp_path)
        await repository.initialize()
        first, _, _, _ = await _run_async(
            tmp_path,
            first_inputs,
            repository=repository,
            notifications=notifications,
            sender=sender,
        )
        second, _, _, _ = await _run_async(
            tmp_path,
            drifted_inputs,
            repository=repository,
            notifications=notifications,
            sender=sender,
        )

        assert first.plan.material_hash == second.plan.material_hash
        assert first.plan.plan_id != second.plan.plan_id
        assert second.outcome == "duplicate"
        assert len(sender.messages) == 1

    asyncio.run(exercise())


def test_material_revision_supersedes_previous_recommendation(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        first_inputs = _inputs(
            _profile(starter_ids=("p1", None)),
            projections=_projections(p1_expected=10.0, p2_expected=30.0),
        )
        later_start = NOW + timedelta(hours=4)
        later_game = ScheduledGame(
            provider_id="g1",
            start_time=later_start,
            status=GameStatus.SCHEDULED,
            home_team_id="12",
            away_team_id="14",
            status_detail=None,
            source=_source(),
        )
        revised_inputs = LivePlanningInputs(
            league_profile=_profile(starter_ids=("p1", None)),
            week_window=FantasyWeekWindow(1, WINDOW_START, WINDOW_END),
            freshness_policy=PlanningFreshnessPolicy(
                max_sleeper_age=timedelta(hours=6),
                max_nba_schedule_age=timedelta(hours=6),
                max_availability_age=timedelta(hours=3),
            ),
            runtime_policy_version=first_inputs.runtime_policy_version,
            move_lead_time=first_inputs.move_lead_time,
            player_eligibility=(
                PlayerEligibilityEvidence("p1", ("PG", "SG"), RETRIEVED_AT, "sleeper-players"),
                PlayerEligibilityEvidence("p2", ("C", "UTIL"), RETRIEVED_AT, "sleeper-players"),
            ),
            identities=(
                ResolvedPlayerIdentity("p1", "401", "12", "override", "high", "override"),
                ResolvedPlayerIdentity("p2", "402", "12", "override", "high", "override"),
            ),
            schedule_results=(
                ScheduleResourceResult("espn:team-schedule:12", (later_game,), _quality()),
            ),
            availability_results=first_inputs.availability_results,
            projections=_projections(p1_expected=10.0, p2_expected=30.0),
        )
        repository, sender, notifications = _workflow(tmp_path)
        await repository.initialize()
        first, _, _, _ = await _run_async(
            tmp_path,
            first_inputs,
            repository=repository,
            notifications=notifications,
            sender=sender,
        )
        second, _, _, _ = await _run_async(
            tmp_path,
            revised_inputs,
            repository=repository,
            notifications=notifications,
            sender=sender,
        )

        assert first.outcome == "notified"
        assert second.outcome == "notified"
        assert len(sender.messages) == 2
        assert first.plan.material_hash != second.plan.material_hash
        superseded = await repository.get_recommendation(first.recommendation.recommendation_id)
        assert superseded is not None
        assert superseded.status is RecommendationStatus.SUPERSEDED

    asyncio.run(exercise())


def test_aba_lifecycle_reactivates_superseded_material_plan(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        plan_a = _inputs(
            _profile(starter_ids=("p1", None)),
            projections=_projections(p1_expected=10.0, p2_expected=30.0),
        )
        later_start = NOW + timedelta(hours=4)
        later_game = ScheduledGame(
            provider_id="g1",
            start_time=later_start,
            status=GameStatus.SCHEDULED,
            home_team_id="12",
            away_team_id="14",
            status_detail=None,
            source=_source(),
        )
        plan_b = LivePlanningInputs(
            league_profile=_profile(starter_ids=("p1", None)),
            week_window=FantasyWeekWindow(1, WINDOW_START, WINDOW_END),
            freshness_policy=PlanningFreshnessPolicy(
                max_sleeper_age=timedelta(hours=6),
                max_nba_schedule_age=timedelta(hours=6),
                max_availability_age=timedelta(hours=3),
            ),
            runtime_policy_version=plan_a.runtime_policy_version,
            move_lead_time=plan_a.move_lead_time,
            player_eligibility=(
                PlayerEligibilityEvidence("p1", ("PG", "SG"), RETRIEVED_AT, "sleeper-players"),
                PlayerEligibilityEvidence("p2", ("C", "UTIL"), RETRIEVED_AT, "sleeper-players"),
            ),
            identities=(
                ResolvedPlayerIdentity("p1", "401", "12", "override", "high", "override"),
                ResolvedPlayerIdentity("p2", "402", "12", "override", "high", "override"),
            ),
            schedule_results=(
                ScheduleResourceResult("espn:team-schedule:12", (later_game,), _quality()),
            ),
            availability_results=plan_a.availability_results,
            projections=_projections(p1_expected=10.0, p2_expected=30.0),
        )
        repository, sender, notifications = _workflow(tmp_path)
        await repository.initialize()
        first, _, _, _ = await _run_async(
            tmp_path,
            plan_a,
            repository=repository,
            notifications=notifications,
            sender=sender,
        )
        second, _, _, _ = await _run_async(
            tmp_path,
            plan_b,
            repository=repository,
            notifications=notifications,
            sender=sender,
        )
        third, _, _, _ = await _run_async(
            tmp_path,
            plan_a,
            repository=repository,
            notifications=notifications,
            sender=sender,
        )

        assert [first.outcome, second.outcome, third.outcome] == [
            "notified",
            "notified",
            "notified",
        ]
        assert len(sender.messages) == 3
        assert first.plan.material_hash == third.plan.material_hash
        assert first.recommendation is not None
        assert third.recommendation is not None
        assert first.recommendation.recommendation_id != third.recommendation.recommendation_id
        pending = await repository.list_pending_recommendations(
            "league-1",
            1,
            decision_type=WEEKLY_LINEUP_DECISION_TYPE,
        )
        assert len(pending) == 1
        assert pending[0].recommendation_id == third.recommendation.recommendation_id
        superseded_a = await repository.get_recommendation(first.recommendation.recommendation_id)
        assert superseded_a is not None
        assert superseded_a.status is RecommendationStatus.SUPERSEDED

    asyncio.run(exercise())


def test_provider_degradation_emits_blocked_health_without_moves(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        stale_profile = _profile(retrieved_at=NOW - timedelta(hours=12))
        inputs = _inputs(stale_profile, projections=_projections())
        repository, sender, notifications = _workflow(tmp_path)
        await repository.initialize()
        first, _, _, _ = await _run_async(
            tmp_path,
            inputs,
            repository=repository,
            notifications=notifications,
            sender=sender,
        )
        second, _, _, _ = await _run_async(
            tmp_path,
            inputs,
            repository=repository,
            notifications=notifications,
            sender=sender,
        )

        assert first.outcome == "blocked"
        assert "Advice is blocked" in sender.messages[0].message
        assert "Start" not in sender.messages[0].message
        assert second.outcome == "duplicate"
        assert len(sender.messages) == 1

    asyncio.run(exercise())
