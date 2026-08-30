"""Coverage for diagnostic automatic assignments and model result construction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from lock_in_diagnostic_support import BASE, _team_week

from sleeper_manager.backtesting.experiments.lock_in_diagnostic_adapter import (
    DiagnosticPolicyAdapter,
    planning_state_for,
    realized_decision_time,
)
from sleeper_manager.backtesting.experiments.lock_in_diagnostic_models import (
    AutomaticSlotAssignment,
    LockInDiagnosticError,
    LockInDiagnosticRequest,
)
from sleeper_manager.backtesting.experiments.lock_in_diagnostic_results import (
    build_model_result,
    legal_automatic_assignments,
)
from sleeper_manager.decisions.lock_in import LockInPolicyConfig
from sleeper_manager.domain.planning import (
    FixedSlot,
    GameOpportunity,
    ObservedStarter,
    PlanningGameStatus,
    PlanningQuality,
    StarterSlot,
    TeamWeekState,
)
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot

NOW = datetime(2026, 1, 10, 18, tzinfo=UTC)


def _projection(player_id: str, game_id: str, expected: float) -> ProjectionSnapshot:
    """Build one point-in-time deterministic projection."""

    return ProjectionSnapshot(
        player_id=player_id,
        game_id=game_id,
        available_as_of=NOW - timedelta(hours=1),
        model_version="fixture",
        input_version=f"inputs-{game_id}",
        scoring_policy_version="scoring",
        distribution=ProjectionDistribution.from_weighted_observations(((expected, 1.0),)),
        reasons=(),
    )


def _opportunity(
    player_id: str,
    game_id: str,
    expected: float,
    *,
    actual: float | None = None,
    slot_indices: tuple[int, ...] = (0,),
    start_offset: int = 1,
) -> GameOpportunity:
    """Build a finalized opportunity with explicit slot eligibility."""

    finalized = actual is not None
    return GameOpportunity(
        sleeper_player_id=player_id,
        provider_player_id=f"provider-{player_id}",
        game_id=game_id,
        scheduled_start=NOW + timedelta(hours=start_offset),
        status=PlanningGameStatus.FINAL if finalized else PlanningGameStatus.SCHEDULED,
        roster_id=1,
        membership_segment="segment-1",
        eligible_slot_indices=slot_indices,
        eligible_positions=("PG",),
        rostered_at_tipoff=True,
        availability_status="available",
        availability_evidence_at=NOW - timedelta(hours=1),
        projection=_projection(player_id, game_id, expected),
        missing_projection_reason=None,
        completed_fantasy_score=actual,
        finalized_at=NOW - timedelta(minutes=1) if finalized else None,
    )


def _state(
    opportunities: tuple[GameOpportunity, ...],
    *,
    slots: tuple[StarterSlot, ...] = (StarterSlot(0, "PG"), StarterSlot(1, "PG")),
    observed: tuple[ObservedStarter, ...] = (),
    fixed_slots: tuple[FixedSlot, ...] = (),
) -> TeamWeekState:
    """Build validated team-week state around supplied automatic-assignment evidence."""

    return TeamWeekState(
        league_id="league",
        season="2026",
        week=1,
        roster_id=1,
        decision_time=NOW,
        starter_slots=slots,
        roster_player_ids=tuple(
            sorted({opportunity.sleeper_player_id for opportunity in opportunities})
        ),
        observed_starters=observed,
        opportunities=opportunities,
        fixed_slots=fixed_slots,
        scoring_policy_version="scoring-v1",
        league_configuration_version="league-v1",
        manager_policy_version="manager-v1",
        projection_model_version="fixture",
        input_version="team-week-inputs-v1",
        eligibility_quality=PlanningQuality.EXACT,
    )


def test_results_assign_negative_automatic_scores_with_full_cardinality() -> None:
    """Fill every open starter slot even when the final legal score is negative."""

    request = LockInDiagnosticRequest(
        _team_week(p1_actual=30, p2_expected=1, p2_actual=-4),
        policy_config=LockInPolicyConfig(scenario_count=64, seed=3),
        planning_cutoffs=(BASE + timedelta(hours=3),),
    )
    adapter = DiagnosticPolicyAdapter(request)
    adapter.run()
    week_end = realized_decision_time(request.team_week)
    model_state = planning_state_for(request, adapter.state, week_end)

    automatic = legal_automatic_assignments(model_state)
    model = build_model_result(
        request,
        locked_slots=model_state.fixed_slots,
        policy_traces=tuple(adapter.policy_traces),
        automatic=automatic,
    )

    assert any(item.score < 0 for item in automatic)
    assert model.realized_score == 26
    covered = {item.slot_index for item in automatic} | {
        slot.slot_index for slot in model.locked_slots
    }
    assert covered == {0, 1}


def test_negative_automatic_score_from_planning_state_directly() -> None:
    """Pin full-cardinality automatic assignment on the shared planning boundary."""

    locked = FixedSlot(
        0,
        "PG",
        "p1",
        "g1",
        30,
        NOW - timedelta(minutes=2),
        "lock-1",
        "fixture",
    )
    state = _state(
        (
            _opportunity("p1", "g1", 30, actual=30, slot_indices=(0,), start_offset=-3),
            _opportunity("p2", "g2", -4, actual=-4, slot_indices=(1,), start_offset=-2),
        ),
        observed=(
            ObservedStarter(0, "p1", ("PG",)),
            ObservedStarter(1, "p2", ("PG",)),
        ),
        fixed_slots=(locked,),
    )

    automatic = legal_automatic_assignments(state)

    assert automatic == (
        AutomaticSlotAssignment(
            slot_index=1,
            slot_position="PG",
            sleeper_id="p2",
            game_id="g2",
            score=-4.0,
        ),
    )


def test_infeasible_automatic_remainder_fails_closed() -> None:
    """Reject a locked sequence whose unlocked starter cannot fill the open slot."""

    locked = FixedSlot(
        0,
        "PG",
        "p1",
        "g1",
        20,
        NOW - timedelta(minutes=2),
        "lock-1",
        "fixture",
    )
    state = _state(
        (
            _opportunity("p1", "g1", 20, actual=20, slot_indices=(0,), start_offset=-3),
            _opportunity("p2", "g2", 5, actual=5, slot_indices=(0,), start_offset=-2),
        ),
        slots=(StarterSlot(0, "PG"), StarterSlot(1, "UTIL")),
        observed=(
            ObservedStarter(0, "p1", ("PG",)),
            ObservedStarter(1, "p2", ("PG",)),
        ),
        fixed_slots=(locked,),
    )

    with pytest.raises(LockInDiagnosticError, match="positionally infeasible|open slot empty"):
        legal_automatic_assignments(state)
