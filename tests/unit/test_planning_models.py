from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.domain.planning import (
    FixedSlot,
    FreshnessSummary,
    GameOpportunity,
    ObservedStarter,
    PassedOpportunity,
    PlanningGameStatus,
    PlanningReasonCode,
    PlanningStateError,
    SourceLineage,
    StarterSlot,
    TeamWeekState,
)
from sleeper_manager.domain.projection import (
    ProjectionDistribution,
    ProjectionSnapshot,
)

NOW = datetime(2026, 1, 5, 12, tzinfo=UTC)


def _projection(
    player_id: str = "p1",
    game_id: str = "g1",
    *,
    available_as_of: datetime = NOW,
) -> ProjectionSnapshot:
    distribution = ProjectionDistribution(
        expected_value=10,
        median=10,
        percentiles=((50, 10),),
        lower_bound=10,
        upper_bound=10,
        variance=0,
    )
    return ProjectionSnapshot(
        player_id=player_id,
        game_id=game_id,
        available_as_of=available_as_of,
        model_version="model-v1",
        input_version="inputs-v1",
        scoring_policy_version="scoring-v1",
        distribution=distribution,
        reasons=(),
    )


def _opportunity(
    player_id: str = "p1",
    game_id: str = "g1",
    *,
    start: datetime = NOW + timedelta(hours=1),
    status: PlanningGameStatus = PlanningGameStatus.SCHEDULED,
    score: float | None = None,
    finalized_at: datetime | None = None,
    projection: ProjectionSnapshot | None = None,
    missing_projection_reason: PlanningReasonCode | None = PlanningReasonCode.MISSING_PROJECTION,
    positions: tuple[str, ...] = ("PG",),
    slot_indices: tuple[int, ...] = (0,),
) -> GameOpportunity:
    return GameOpportunity(
        sleeper_player_id=player_id,
        provider_player_id=f"provider-{player_id}",
        game_id=game_id,
        scheduled_start=start,
        status=status,
        roster_id=1,
        membership_segment="segment-1",
        eligible_slot_indices=slot_indices,
        eligible_positions=positions,
        rostered_at_tipoff=True,
        availability_status="available",
        availability_evidence_at=NOW,
        projection=projection,
        missing_projection_reason=missing_projection_reason,
        completed_fantasy_score=score,
        finalized_at=finalized_at,
        source_lineage=(SourceLineage("replay", "inputs-v1", NOW, NOW),),
    )


def _state(
    opportunities: tuple[GameOpportunity, ...],
    *,
    observed_starters: tuple[ObservedStarter, ...] = (),
    fixed_slots: tuple[FixedSlot, ...] = (),
    passed_opportunities: tuple[PassedOpportunity, ...] = (),
    blocking_reasons: tuple[PlanningReasonCode, ...] = (),
    decision_time: datetime = NOW,
) -> TeamWeekState:
    return TeamWeekState(
        league_id="league-1",
        season="2026",
        week=1,
        roster_id=1,
        decision_time=decision_time,
        starter_slots=(StarterSlot(0, "G"), StarterSlot(1, "UTIL")),
        roster_player_ids=("p1", "p2"),
        observed_starters=observed_starters,
        opportunities=opportunities,
        fixed_slots=fixed_slots,
        passed_opportunities=passed_opportunities,
        scoring_policy_version="scoring-v1",
        league_configuration_version="league-v1",
        manager_policy_version="policy-v1",
        projection_model_version="model-v1",
        input_version="inputs-v1",
        freshness=FreshnessSummary((SourceLineage("replay", "inputs-v1", NOW, NOW),)),
        blocking_reasons=blocking_reasons,
    )


def test_duplicate_starter_slots_fail() -> None:
    with pytest.raises(PlanningStateError, match="starter slots"):
        TeamWeekState(
            league_id="league-1",
            season="2026",
            week=1,
            roster_id=1,
            decision_time=NOW,
            starter_slots=(StarterSlot(0, "G"), StarterSlot(0, "UTIL")),
            roster_player_ids=("p1",),
            observed_starters=(),
            opportunities=(_opportunity(),),
        )


def test_timezone_naive_timestamps_fail() -> None:
    with pytest.raises(PlanningStateError, match="timezone-aware"):
        _opportunity(start=datetime(2026, 1, 5, 13))


def test_duplicate_player_game_opportunities_fail() -> None:
    with pytest.raises(PlanningStateError, match="player-game"):
        _state((_opportunity(), _opportunity()))


def test_duplicate_fixed_assignments_fail() -> None:
    opportunity = _opportunity(
        status=PlanningGameStatus.FINAL,
        score=12,
        finalized_at=NOW - timedelta(minutes=1),
    )
    fixed = FixedSlot(0, "G", "p1", "g1", 12, NOW, "decision-1", "replay")
    with pytest.raises(PlanningStateError, match="reuse a slot"):
        _state((opportunity,), fixed_slots=(fixed, fixed))


def test_future_projection_availability_fails() -> None:
    opportunity = _opportunity(
        projection=_projection(available_as_of=NOW + timedelta(minutes=1)),
        missing_projection_reason=None,
    )
    with pytest.raises(PlanningStateError, match="newer"):
        _state((opportunity,))


def test_multi_position_observed_starter_is_valid_for_selected_slot() -> None:
    state = _state(
        (_opportunity(positions=("PG", "SG"), slot_indices=(0,)),),
        observed_starters=(ObservedStarter(0, "p1", ("PG", "SG")),),
    )
    assert state.observed_starters[0].slot_index == 0


def test_passed_game_does_not_suppress_later_game() -> None:
    first = _opportunity()
    later = _opportunity(
        game_id="g2",
        start=NOW + timedelta(days=1),
        slot_indices=(0,),
    )
    state = _state(
        (first, later),
        passed_opportunities=(PassedOpportunity("p1", "g1", NOW, "decision-1", "replay"),),
    )
    assert tuple(opportunity.game_id for opportunity in state.unpassed_opportunities) == ("g2",)


def test_fixed_slot_is_excluded_from_open_slots() -> None:
    opportunity = _opportunity(
        status=PlanningGameStatus.FINAL,
        score=12,
        finalized_at=NOW - timedelta(minutes=1),
    )
    state = _state(
        (opportunity,),
        fixed_slots=(FixedSlot(0, "G", "p1", "g1", 12, NOW, "decision-1", "replay"),),
    )
    assert state.open_slot_indices == (1,)


def test_blocked_state_retains_reason_codes_and_source_coverage() -> None:
    state = _state(
        (_opportunity(),),
        blocking_reasons=(
            PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY,
            PlanningReasonCode.MISSING_PROJECTION,
        ),
    )
    assert state.is_blocked
    assert state.blocking_reasons == (
        PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY,
        PlanningReasonCode.MISSING_PROJECTION,
    )
    assert state.freshness.sources[0].source == "replay"
