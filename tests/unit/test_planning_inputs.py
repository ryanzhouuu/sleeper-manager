from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.backtesting.replay.engine import ReplayConfig
from sleeper_manager.backtesting.replay.models import ReplayGame, ReplayGameStatus
from sleeper_manager.backtesting.replay.models import (
    ReplayPlayerGame as ReplayPlayerGameRecord,
)
from sleeper_manager.backtesting.replay.planning_adapter import team_week_state_from_replay
from sleeper_manager.backtesting.replay.state import ReplayState
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
    AcknowledgedAction,
    AcknowledgedDecisionEvidence,
    PlanningGameStatus,
    PlanningQuality,
    PlanningReasonCode,
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
    PlanningInputsError,
    PlayerEligibilityEvidence,
    ResolvedPlayerIdentity,
    ScheduleResourceResult,
    build_live_team_week_state,
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
) -> LivePlanningInputs:
    return LivePlanningInputs(
        league_profile=profile or _profile(),
        week_window=FantasyWeekWindow(1, WINDOW_START, WINDOW_END),
        freshness_policy=freshness_policy or _freshness_policy(),
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


def test_complete_inputs_build_validated_state() -> None:
    inputs = _inputs(
        projections=(
            LiveProjectionResult("p1", "g1", _snapshot("p1", "g1"), None),
            LiveProjectionResult("p1", "g2", _snapshot("p1", "g2"), None),
        ),
    )
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert not state.is_blocked
    assert state.eligibility_quality is PlanningQuality.EXACT
    assert state.week == 1
    assert state.roster_player_ids == ("p1", "p2")
    assert [slot.position for slot in state.starter_slots] == ["PG", "UTIL"]
    assert [(starter.slot_index, starter.player_id) for starter in state.observed_starters] == [
        (0, "p1"),
        (1, "p2"),
    ]
    assert {opportunity.game_id for opportunity in state.opportunities} == {"g1", "g2"}
    first = next(opportunity for opportunity in state.opportunities if opportunity.game_id == "g1")
    assert first.status is PlanningGameStatus.SCHEDULED
    assert first.eligible_slot_indices == (0, 1)
    assert first.projection is not None and first.projection.model_version == "baseline-v1"
    assert first.availability_status == "questionable"
    assert state.projection_model_version == "baseline-v1"
    assert state.scoring_policy_version.startswith("scoring-policy-v1-")


def test_week_window_scopes_games_to_the_fantasy_week() -> None:
    inside = _game()
    outside = _game("g9", start=WINDOW_END + timedelta(days=1))
    inputs = _inputs(schedule_results=(_schedule_result(inside, outside),))
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert {opportunity.game_id for opportunity in state.opportunities} == {"g1"}


def test_bundle_rejects_window_profile_mismatch() -> None:
    profile = _profile()
    stale_window = FantasyWeekWindow(2, WINDOW_START, WINDOW_END)
    with pytest.raises(PlanningInputsError, match="does not match"):
        LivePlanningInputs(
            league_profile=profile,
            week_window=stale_window,
            freshness_policy=_freshness_policy(),
        )


def test_unresolved_identity_blocks_affected_player() -> None:
    inputs = _inputs()
    object.__setattr__(inputs, "identities", (_identity("p1"),))
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY in state.blocking_reasons
    p2_games = [
        opportunity for opportunity in state.opportunities if opportunity.sleeper_player_id == "p2"
    ]
    assert p2_games == []


def test_missing_schedule_for_player_team_is_reported() -> None:
    identity = ResolvedPlayerIdentity(
        sleeper_player_id="p2",
        provider_player_id="402",
        provider_team_id=None,
        method="name_only",
        confidence="low",
        reason="no team evidence",
    )
    inputs = _inputs()
    object.__setattr__(inputs, "identities", (_identity("p1"), identity))
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert PlanningReasonCode.MISSING_GAME_SCHEDULE in state.blocking_reasons


def test_missing_projection_keeps_candidate_with_reason() -> None:
    state = build_live_team_week_state(_inputs(), decision_time=NOW)

    first = next(opportunity for opportunity in state.opportunities if opportunity.game_id == "g1")
    assert first.projection is None
    assert first.missing_projection_reason is PlanningReasonCode.MISSING_PROJECTION
    assert PlanningReasonCode.MISSING_PROJECTION not in state.blocking_reasons


def test_projection_after_decision_time_is_blocked() -> None:
    late_snapshot = _snapshot("p1", "g1", available_as_of=NOW + timedelta(minutes=1))
    inputs = _inputs(projections=(LiveProjectionResult("p1", "g1", late_snapshot, None),))
    state = build_live_team_week_state(inputs, decision_time=NOW)

    first = next(opportunity for opportunity in state.opportunities if opportunity.game_id == "g1")
    assert first.missing_projection_reason is PlanningReasonCode.PROJECTION_AFTER_DECISION
    assert PlanningReasonCode.PROJECTION_AFTER_DECISION in state.blocking_reasons


def test_stale_sleeper_state_blocks_planning() -> None:
    old_profile = _profile(retrieved_at=NOW - timedelta(hours=12))
    inputs = _inputs(old_profile)
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert state.is_blocked
    assert PlanningReasonCode.STALE_SLEEPER_STATE in state.blocking_reasons


def test_duplicate_team_schedules_with_equal_facts_merge_cleanly() -> None:
    later_fetch = _game()
    object.__setattr__(
        later_fetch,
        "source",
        SourceMetadata(provider="espn", provider_id="x", retrieved_at=NOW - timedelta(seconds=30)),
    )
    inputs = _inputs(
        schedule_results=(
            ScheduleResourceResult(
                resource="espn:team-schedule:12",
                games=(_game(),),
                quality=_quality(resource="espn:team-schedule:12"),
            ),
            ScheduleResourceResult(
                resource="espn:team-schedule:14",
                games=(later_fetch,),
                quality=_quality(resource="espn:team-schedule:14"),
            ),
        ),
    )
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert PlanningReasonCode.AMBIGUOUS_EVENT_ORDER not in state.blocking_reasons
    assert len(state.opportunities) >= 1
    first_lineage = next(
        opportunity for opportunity in state.opportunities if opportunity.game_id == "g1"
    ).source_lineage
    lineage_sources = {item.source for item in first_lineage}
    assert any("team-schedule:12" in source for source in lineage_sources)
    assert any("team-schedule:14" in source for source in lineage_sources)


def test_conflicting_game_facts_still_block() -> None:
    conflicting = _game(start=NOW + timedelta(hours=5))
    inputs = _inputs(
        schedule_results=(
            _schedule_result(_game()),
            ScheduleResourceResult(
                resource="espn:team-schedule:14",
                games=(conflicting,),
                quality=_quality(resource="espn:team-schedule:14"),
            ),
        ),
    )
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert PlanningReasonCode.AMBIGUOUS_EVENT_ORDER in state.blocking_reasons


def test_stale_eligibility_blocks_planning() -> None:
    old_eligibility = PlayerEligibilityEvidence(
        sleeper_player_id="p1",
        eligible_positions=("PG",),
        available_as_of=NOW - timedelta(hours=48),
        provenance="sleeper-player-catalog",
    )
    fresh = PlayerEligibilityEvidence(
        sleeper_player_id="p2",
        eligible_positions=("C",),
        available_as_of=RETRIEVED_AT,
        provenance="sleeper-player-catalog",
    )
    inputs = _inputs()
    object.__setattr__(inputs, "player_eligibility", (old_eligibility, fresh))
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert PlanningReasonCode.STALE_SLEEPER_STATE in state.blocking_reasons
    assert all(opportunity.sleeper_player_id != "p1" for opportunity in state.opportunities)


def test_future_dated_eligibility_is_unusable() -> None:
    future = PlayerEligibilityEvidence(
        sleeper_player_id="p1",
        eligible_positions=("PG",),
        available_as_of=NOW + timedelta(minutes=1),
        provenance="sleeper-player-catalog",
    )
    fresh = PlayerEligibilityEvidence(
        sleeper_player_id="p2",
        eligible_positions=("C",),
        available_as_of=RETRIEVED_AT,
        provenance="sleeper-player-catalog",
    )
    inputs = _inputs()
    object.__setattr__(inputs, "player_eligibility", (future, fresh))
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert PlanningReasonCode.AMBIGUOUS_ELIGIBILITY in state.blocking_reasons
    assert all(opportunity.sleeper_player_id != "p1" for opportunity in state.opportunities)


def test_provider_stale_quality_warns_within_age_limit() -> None:
    stale_labeled = ScheduleResourceResult(
        resource="espn:team-schedule:12",
        games=(_game(),),
        quality=_quality(DataQualityState.STALE, resource="espn:team-schedule:12"),
    )
    inputs = _inputs(schedule_results=(stale_labeled,))
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert not state.is_blocked
    assert PlanningReasonCode.STALE_NBA_STATE in state.warnings


def test_identity_resource_failure_blocks_planning() -> None:
    failure_report = DataQualityReport(
        state=DataQualityState.ERROR,
        resource="nba:team-roster:CHI",
        record_count=0,
        retrieved_at=RETRIEVED_AT,
        source_updated_at=None,
        expires_at=None,
        errors=("roster fetch failed",),
    )
    inputs = _inputs()
    object.__setattr__(inputs, "identity_quality_reports", (failure_report,))
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert PlanningReasonCode.STALE_NBA_STATE in state.blocking_reasons
    assert any(source.source == "nba:team-roster:CHI" for source in state.freshness.sources)


def test_stale_and_erroring_nba_resources_block() -> None:
    stale_resource = ScheduleResourceResult(
        resource="espn:team-schedule:12",
        games=(_game(),),
        quality=_quality(retrieved_at=NOW - timedelta(days=2)),
    )
    error_resource = ScheduleResourceResult(
        resource="espn:team-schedule:14",
        games=(),
        quality=_quality(DataQualityState.ERROR, resource="espn:team-schedule:14"),
    )
    inputs = _inputs(schedule_results=(stale_resource, error_resource))
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert PlanningReasonCode.STALE_NBA_STATE in state.blocking_reasons


def test_empty_schedule_is_legitimate_but_error_is_not() -> None:
    empty_inputs = _inputs(
        schedule_results=(
            ScheduleResourceResult(
                resource="espn:team-schedule:12",
                games=(),
                quality=_quality(DataQualityState.EMPTY, resource="espn:team-schedule:12"),
            ),
        ),
    )
    empty_state = build_live_team_week_state(empty_inputs, decision_time=NOW)
    assert empty_state.opportunities == ()
    assert PlanningReasonCode.STALE_NBA_STATE not in empty_state.blocking_reasons

    error_inputs = _inputs(
        schedule_results=(
            ScheduleResourceResult(
                resource="espn:team-schedule:12",
                games=(),
                quality=_quality(DataQualityState.ERROR, resource="espn:team-schedule:12"),
            ),
        ),
    )
    error_state = build_live_team_week_state(error_inputs, decision_time=NOW)
    assert PlanningReasonCode.STALE_NBA_STATE in error_state.blocking_reasons


def test_partial_quality_warns_without_blocking() -> None:
    inputs = _inputs(
        schedule_results=(
            ScheduleResourceResult(
                resource="espn:team-schedule:12",
                games=(_game(),),
                quality=_quality(DataQualityState.PARTIAL),
            ),
        ),
    )
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert not state.is_blocked
    assert PlanningReasonCode.STALE_NBA_STATE in state.warnings


def test_conflicting_game_rows_block_as_ambiguous_order() -> None:
    conflicting = _game(start=NOW + timedelta(hours=5))
    inputs = _inputs(
        schedule_results=(
            _schedule_result(_game()),
            ScheduleResourceResult(
                resource="espn:scoreboard",
                games=(conflicting,),
                quality=_quality(resource="espn:scoreboard"),
            ),
        ),
    )
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert PlanningReasonCode.AMBIGUOUS_EVENT_ORDER in state.blocking_reasons


def test_missing_eligibility_excludes_opportunity_and_blocks() -> None:
    inputs = _inputs()
    object.__setattr__(inputs, "player_eligibility", (_eligibility("p1"), _eligibility("p2", ())))
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert PlanningReasonCode.AMBIGUOUS_ELIGIBILITY in state.blocking_reasons
    assert all(opportunity.sleeper_player_id != "p2" for opportunity in state.opportunities)


def test_acknowledged_lock_becomes_fixed_slot() -> None:
    lock = AcknowledgedDecisionEvidence(
        decision_id="rec-lock-1",
        player_id="p1",
        game_id="g1",
        action=AcknowledgedAction.LOCK,
        decided_at=NOW - timedelta(hours=1),
        provenance="notification-token",
        slot_index=0,
        slot_position="PG",
        accepted_fantasy_score=24,
    )
    inputs = _inputs(
        projections=(LiveProjectionResult("p1", "g1", _snapshot("p1", "g1"), None),),
        acknowledgements=(lock,),
    )
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert len(state.fixed_slots) == 1
    assert state.open_slot_indices == (1,)
    assert state.fixed_slots[0].accepted_fantasy_score == 24


def test_acknowledged_pass_removes_one_opportunity_only() -> None:
    passed = AcknowledgedDecisionEvidence(
        decision_id="rec-pass-1",
        player_id="p1",
        game_id="g1",
        action=AcknowledgedAction.PASS,
        decided_at=NOW - timedelta(hours=1),
        provenance="notification-token",
    )
    inputs = _inputs(acknowledgements=(passed,))
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert len(state.passed_opportunities) == 1
    remaining = {
        (opportunity.sleeper_player_id, opportunity.game_id)
        for opportunity in state.unpassed_opportunities
    }
    assert ("p1", "g1") not in remaining
    assert ("p1", "g2") in remaining


def test_conflicting_acknowledgements_block_without_conversions() -> None:
    first_lock = AcknowledgedDecisionEvidence(
        decision_id="rec-a",
        player_id="p1",
        game_id="g1",
        action=AcknowledgedAction.LOCK,
        decided_at=NOW - timedelta(hours=2),
        provenance="token",
        slot_index=0,
        slot_position="PG",
        accepted_fantasy_score=10,
    )
    second_lock = AcknowledgedDecisionEvidence(
        decision_id="rec-b",
        player_id="p2",
        game_id="g2",
        action=AcknowledgedAction.LOCK,
        decided_at=NOW - timedelta(hours=1),
        provenance="token",
        slot_index=0,
        slot_position="PG",
        accepted_fantasy_score=12,
    )
    unreconciled = AcknowledgedDecisionEvidence(
        decision_id="rec-c",
        player_id="p1",
        game_id="g2",
        action=AcknowledgedAction.PASS,
        decided_at=NOW - timedelta(minutes=30),
        provenance="repository-query",
        reconciled=False,
    )
    inputs = _inputs(acknowledgements=(first_lock, second_lock, unreconciled))
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert len(state.fixed_slots) == 1
    assert state.passed_opportunities == ()
    assert PlanningReasonCode.ACKNOWLEDGEMENT_CONFLICT in state.blocking_reasons


def test_lock_with_unknown_slot_becomes_conflict_not_abort() -> None:
    invalid_lock = AcknowledgedDecisionEvidence(
        decision_id="rec-x",
        player_id="p1",
        game_id="g1",
        action=AcknowledgedAction.LOCK,
        decided_at=NOW - timedelta(hours=1),
        provenance="repository-query",
        slot_index=99,
        slot_position="PG",
        accepted_fantasy_score=10,
    )
    inputs = _inputs(acknowledgements=(invalid_lock,))
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert state.fixed_slots == ()
    assert PlanningReasonCode.ACKNOWLEDGEMENT_CONFLICT in state.blocking_reasons


def test_lock_with_wrong_slot_position_becomes_conflict() -> None:
    misplaced = AcknowledgedDecisionEvidence(
        decision_id="rec-y",
        player_id="p1",
        game_id="g1",
        action=AcknowledgedAction.LOCK,
        decided_at=NOW - timedelta(hours=1),
        provenance="repository-query",
        slot_index=1,
        slot_position="PG",
        accepted_fantasy_score=10,
    )
    inputs = _inputs(acknowledgements=(misplaced,))
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert state.fixed_slots == ()
    assert PlanningReasonCode.ACKNOWLEDGEMENT_CONFLICT in state.blocking_reasons


def test_lock_for_ineligible_player_becomes_conflict() -> None:
    invalid_lock = AcknowledgedDecisionEvidence(
        decision_id="rec-ineligible",
        player_id="p2",
        game_id="g1",
        action=AcknowledgedAction.LOCK,
        decided_at=NOW - timedelta(hours=1),
        provenance="repository-query",
        slot_index=0,
        slot_position="PG",
        accepted_fantasy_score=10,
    )
    inputs = _inputs(acknowledgements=(invalid_lock,))
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert state.fixed_slots == ()
    assert PlanningReasonCode.ACKNOWLEDGEMENT_CONFLICT in state.blocking_reasons


def test_future_dated_acknowledgement_becomes_conflict() -> None:
    future_pass = AcknowledgedDecisionEvidence(
        decision_id="rec-z",
        player_id="p1",
        game_id="g1",
        action=AcknowledgedAction.PASS,
        decided_at=NOW + timedelta(minutes=1),
        provenance="repository-query",
    )
    inputs = _inputs(acknowledgements=(future_pass,))
    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert state.passed_opportunities == ()
    assert PlanningReasonCode.ACKNOWLEDGEMENT_CONFLICT in state.blocking_reasons


def test_naive_decision_time_is_rejected() -> None:
    with pytest.raises(PlanningInputsError, match="timezone-aware"):
        build_live_team_week_state(_inputs(), decision_time=datetime(2026, 1, 7, 18))


def test_naive_bundle_timestamps_are_rejected() -> None:
    with pytest.raises(PlanningInputsError, match="timezone-aware"):
        FantasyWeekWindow(1, datetime(2026, 1, 5), WINDOW_END)


def test_live_and_replay_states_agree_semantically() -> None:
    decision_time = NOW + timedelta(hours=2)
    tipoff = NOW - timedelta(hours=20)

    def snapshot_for(player_id: str, game_id: str) -> ProjectionSnapshot:
        return ProjectionSnapshot(
            player_id=player_id,
            game_id=game_id,
            available_as_of=decision_time,
            model_version="baseline-v1",
            input_version="live-inputs-v1",
            scoring_policy_version="scoring-v1",
            distribution=_distribution(),
            reasons=(),
        )

    live_inputs = _inputs(
        projections=tuple(
            LiveProjectionResult(player_id, game_id, snapshot_for(player_id, game_id), None)
            for player_id in ("p1", "p2")
            for game_id in ("g1", "g2")
        ),
    )
    live_state = build_live_team_week_state(live_inputs, decision_time=decision_time)

    replay_config = ReplayConfig(starter_slots=("PG", "UTIL"), league_id="league-1", week=1)
    replay_state = ReplayState(
        starter_slots=("PG", "UTIL"),
        games=(
            ReplayGame(
                game_id="g1",
                start_time=tipoff,
                final_time=None,
                week=1,
                team_ids=("12", "14"),
                status=ReplayGameStatus.SCHEDULED,
            ),
            ReplayGame(
                game_id="g2",
                start_time=tipoff + timedelta(days=2),
                final_time=None,
                week=1,
                team_ids=("12", "16"),
                status=ReplayGameStatus.SCHEDULED,
            ),
        ),
        player_games=(
            ReplayPlayerGameRecord(
                sleeper_id="p1",
                provider_player_id="401",
                game_id="g1",
                fantasy_team_id=1,
                rostered_at_tipoff=True,
                eligible_positions=("PG", "SG"),
                actual_score=0,
                projection=snapshot_for("p1", "g1"),
            ),
            ReplayPlayerGameRecord(
                sleeper_id="p1",
                provider_player_id="401",
                game_id="g2",
                fantasy_team_id=1,
                rostered_at_tipoff=True,
                eligible_positions=("PG", "SG"),
                actual_score=0,
                projection=snapshot_for("p1", "g2"),
            ),
            ReplayPlayerGameRecord(
                sleeper_id="p2",
                provider_player_id="402",
                game_id="g1",
                fantasy_team_id=1,
                rostered_at_tipoff=True,
                eligible_positions=("C",),
                actual_score=0,
                projection=snapshot_for("p2", "g1"),
            ),
            ReplayPlayerGameRecord(
                sleeper_id="p2",
                provider_player_id="402",
                game_id="g2",
                fantasy_team_id=1,
                rostered_at_tipoff=True,
                eligible_positions=("C",),
                actual_score=0,
                projection=snapshot_for("p2", "g2"),
            ),
        ),
    )
    profile = _profile()
    replay_domain_state = team_week_state_from_replay(
        replay_state,
        config=replay_config,
        decision_time=decision_time,
        league_profile=profile,
        roster_player_ids=("p1", "p2"),
    )

    assert live_state.starter_slots == replay_domain_state.starter_slots
    assert live_state.roster_player_ids == replay_domain_state.roster_player_ids
    assert [
        (starter.slot_index, starter.player_id, starter.eligible_positions)
        for starter in live_state.observed_starters
    ] == [
        (starter.slot_index, starter.player_id, starter.eligible_positions)
        for starter in replay_domain_state.observed_starters
    ]
    assert {(item.sleeper_player_id, item.game_id) for item in live_state.opportunities} == {
        (item.sleeper_player_id, item.game_id) for item in replay_domain_state.opportunities
    }
    live_by_key = {
        (item.sleeper_player_id, item.game_id): item for item in live_state.opportunities
    }
    replay_by_key = {
        (item.sleeper_player_id, item.game_id): item for item in replay_domain_state.opportunities
    }
    for key, live_opportunity in live_by_key.items():
        replay_opportunity = replay_by_key[key]
        assert live_opportunity.status == replay_opportunity.status
        assert live_opportunity.eligible_slot_indices == replay_opportunity.eligible_slot_indices
        assert live_opportunity.eligible_positions == replay_opportunity.eligible_positions
        assert live_opportunity.provider_player_id == replay_opportunity.provider_player_id
    assert live_state.fixed_slots == replay_domain_state.fixed_slots
    assert live_state.passed_opportunities == replay_domain_state.passed_opportunities
    assert live_state.blocking_reasons == replay_domain_state.blocking_reasons

    assert all(lineage.source != "replay" for lineage in live_state.freshness.sources)
    assert any(lineage.source == "replay" for lineage in replay_domain_state.freshness.sources)
