"""Live team-week state assembly from complete and degraded planning inputs."""

from datetime import timedelta

import pytest

from sleeper_manager.domain.nba import (
    DataQualityReport,
    DataQualityState,
    GameStatus,
    SourceMetadata,
)
from sleeper_manager.domain.planning import (
    PlanningGameStatus,
    PlanningQuality,
    PlanningReasonCode,
)
from sleeper_manager.workflows.planning_inputs import (
    FantasyWeekWindow,
    LivePlanningInputs,
    LiveProjectionResult,
    PlanningInputsError,
    PlayerEligibilityEvidence,
    ResolvedPlayerIdentity,
    ScheduleResourceResult,
    build_live_team_week_state,
)
from tests.sleeper_manager.workflows.planning_inputs_support import (
    NOW,
    RETRIEVED_AT,
    WINDOW_END,
    WINDOW_START,
    _eligibility,
    _freshness_policy,
    _game,
    _identity,
    _inputs,
    _profile,
    _quality,
    _schedule_result,
    _snapshot,
)


def test_complete_inputs_build_validated_state() -> None:
    inputs = _inputs(
        projections=(
            LiveProjectionResult("p1", "g1", _snapshot("p1", "g1"), None),
            LiveProjectionResult("p1", "g2", _snapshot("p1", "g2"), None),
        ),
    )
    state = build_live_team_week_state(inputs, decision_time=NOW)
    assert state.manager_policy_version == "runtime-policy-v7"

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


def test_unknown_games_are_not_treated_as_scheduled() -> None:
    unknown = _game("g-unknown", status=GameStatus.UNKNOWN)
    inputs = _inputs(schedule_results=(_schedule_result(unknown, _game()),))
    state = build_live_team_week_state(inputs, decision_time=NOW)
    unknown_opportunity = next(
        opportunity for opportunity in state.opportunities if opportunity.game_id == "g-unknown"
    )

    assert unknown_opportunity.status is PlanningGameStatus.UNKNOWN
    assert unknown_opportunity not in state.remaining_opportunities


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
            runtime_policy_version="runtime-policy-v7",
            move_lead_time=timedelta(minutes=10),
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
