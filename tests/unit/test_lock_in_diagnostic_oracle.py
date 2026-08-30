"""Coverage for temporal feasibility of historical Lock-In oracle selections."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from lock_in_diagnostic_support import BASE, _team_week

from sleeper_manager.backtesting.experiments.lock_in_diagnostic_adapter import (
    planning_state_for,
    realized_decision_time,
)
from sleeper_manager.backtesting.experiments.lock_in_diagnostic_models import (
    LockInDiagnosticError,
    LockInDiagnosticRequest,
)
from sleeper_manager.backtesting.experiments.lock_in_diagnostic_oracle import (
    oracle_feasibility_checks,
    oracle_from_planning_state,
)
from sleeper_manager.backtesting.replay.models import ReplayGame, TeamWeekReplayResult
from sleeper_manager.backtesting.replay.state import ReplayState
from sleeper_manager.domain.lock_in import LockInDecision, LockInDecisionKind
from sleeper_manager.domain.planning import (
    FixedSlot,
    GameOpportunity,
    PlanningGameStatus,
    PlanningQuality,
    StarterSlot,
    TeamWeekState,
)
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot

NOW = datetime(2026, 1, 10, 18, tzinfo=UTC)


def _planning_state(team_week):
    """Build an unconstrained week-end planning state for oracle fixture tests."""

    request = LockInDiagnosticRequest(team_week)
    week_end = realized_decision_time(team_week)
    return planning_state_for(
        request,
        ReplayState(
            starter_slots=team_week.starter_slots,
            games=team_week.games,
            player_games=team_week.player_games,
        ),
        week_end,
    )


def test_oracle_from_planning_state_selects_best_realized_assignment() -> None:
    """Prefer the higher realized score under full starter-slot cardinality."""

    state = _planning_state(_team_week(p1_actual=30, p2_expected=1, p2_actual=8))

    oracle = oracle_from_planning_state(state)

    assert oracle.realized_score == 38
    assert {slot.player_id for slot in oracle.locked_slots} == {"p1"}


def test_infeasible_oracle_assignment_raises_diagnostic_error() -> None:
    """Keep oracle lineup infeasibility on the diagnostic fail-closed error type."""

    opportunity = GameOpportunity(
        sleeper_player_id="p1",
        provider_player_id="provider-p1",
        game_id="g1",
        scheduled_start=NOW - timedelta(hours=3),
        status=PlanningGameStatus.FINAL,
        roster_id=1,
        membership_segment="segment-1",
        eligible_slot_indices=(0,),
        eligible_positions=("PG",),
        rostered_at_tipoff=True,
        availability_status="available",
        availability_evidence_at=NOW - timedelta(hours=4),
        projection=ProjectionSnapshot(
            player_id="p1",
            game_id="g1",
            available_as_of=NOW - timedelta(hours=4),
            model_version="fixture",
            input_version="inputs-g1",
            scoring_policy_version="scoring",
            distribution=ProjectionDistribution.from_weighted_observations(((10.0, 1.0),)),
            reasons=(),
        ),
        missing_projection_reason=None,
        completed_fantasy_score=10.0,
        finalized_at=NOW - timedelta(minutes=1),
    )
    state = TeamWeekState(
        league_id="league",
        season="2026",
        week=1,
        roster_id=1,
        decision_time=NOW,
        starter_slots=(StarterSlot(0, "PG"), StarterSlot(1, "UTIL")),
        roster_player_ids=("p1",),
        observed_starters=(),
        opportunities=(opportunity,),
        scoring_policy_version="scoring-v1",
        league_configuration_version="league-v1",
        manager_policy_version="manager-v1",
        projection_model_version="fixture",
        input_version="team-week-inputs-v1",
        eligibility_quality=PlanningQuality.EXACT,
    )

    with pytest.raises(LockInDiagnosticError, match="infeasible"):
        oracle_from_planning_state(state)


def test_oracle_feasibility_rejects_earlier_pick_finalized_after_next_game() -> None:
    """Reject oracle locks that finalize after the next rostered game starts."""

    team_week = _team_week()
    games = list(team_week.games)
    g1 = games[0]
    games[0] = ReplayGame(
        g1.game_id,
        g1.start_time,
        BASE + timedelta(hours=13),
        g1.week,
        g1.team_ids,
        g1.status,
    )
    team_week = replace(team_week, games=tuple(games))
    state = _planning_state(team_week)
    oracle = TeamWeekReplayResult(
        league_id=team_week.league_id,
        week=team_week.week,
        roster_id=team_week.roster_id,
        policy_name="oracle",
        realized_score=50,
        decisions=(
            LockInDecision(
                BASE + timedelta(hours=13),
                LockInDecisionKind.LOCK,
                "p1",
                "g1",
                0,
                50,
                50,
                "realized-outcomes",
                "Constrained maximum-weight realized assignment.",
            ),
        ),
        locked_slots=(
            FixedSlot(
                0,
                "UTIL",
                "p1",
                "g1",
                50,
                BASE + timedelta(hours=13),
                "oracle-lock",
                "realized-outcomes",
            ),
        ),
        automatic_final_scores=(("p2", 8.0),),
        eligibility_quality=team_week.eligibility_quality.value,
        data_quality="partial",
    )

    checks = oracle_feasibility_checks(oracle, state)

    assert any(not check.feasible and check.kind == "lockable_earlier" for check in checks)
