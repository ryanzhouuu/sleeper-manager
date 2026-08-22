from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.backtesting.replay import ReplayConfig, ReplayState
from sleeper_manager.backtesting.replay.models import ReplayGame, ReplayGameStatus, ReplayPlayerGame
from sleeper_manager.backtesting.replay.planning_adapter import team_week_state_from_replay
from sleeper_manager.decisions.lineup import SlotAssignment
from sleeper_manager.decisions.weekly_plan import (
    TerminalValueApproximation,
    WeeklyPlanError,
    WeeklyPlanPolicyConfig,
    score_weekly_options,
)
from sleeper_manager.domain.eligibility import eligible_for_slot
from sleeper_manager.domain.planning import (
    FixedSlot,
    FreshnessSummary,
    GameOpportunity,
    ObservedStarter,
    PassedOpportunity,
    PlanningGameStatus,
    PlanningReasonCode,
    SourceLineage,
    StarterSlot,
    TeamWeekState,
)
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot

NOW = datetime(2026, 1, 5, 12, tzinfo=UTC)


def _projection(
    player_id: str,
    game_id: str,
    observations: tuple[tuple[float, float], ...],
) -> ProjectionSnapshot:
    return ProjectionSnapshot(
        player_id=player_id,
        game_id=game_id,
        available_as_of=NOW,
        model_version="fixture-model",
        input_version="fixture-inputs",
        scoring_policy_version="fixture-scoring",
        distribution=ProjectionDistribution.from_weighted_observations(observations),
        reasons=(),
    )


def _opportunity(
    player_id: str,
    game_id: str,
    start: datetime,
    positions: tuple[str, ...],
    observations: tuple[tuple[float, float], ...],
    *,
    status: PlanningGameStatus = PlanningGameStatus.SCHEDULED,
    rostered: bool | None = True,
    completed_score: float | None = None,
    finalized_at: datetime | None = None,
) -> GameOpportunity:
    return GameOpportunity(
        sleeper_player_id=player_id,
        provider_player_id=f"provider-{player_id}",
        game_id=game_id,
        scheduled_start=start,
        status=status,
        roster_id=1,
        membership_segment="segment-1",
        eligible_slot_indices=tuple(
            index for index, slot in enumerate(("G", "UTIL")) if eligible_for_slot(positions, slot)
        ),
        eligible_positions=positions,
        rostered_at_tipoff=rostered,
        availability_status="available",
        availability_evidence_at=NOW,
        projection=_projection(player_id, game_id, observations),
        missing_projection_reason=None,
        completed_fantasy_score=completed_score,
        finalized_at=finalized_at,
        source_lineage=(SourceLineage("fixture", "v1", NOW, NOW),),
    )


def _state(
    opportunities: tuple[GameOpportunity, ...],
    *,
    observed: tuple[ObservedStarter, ...] = (),
    fixed: tuple[FixedSlot, ...] = (),
    passed: tuple[PassedOpportunity, ...] = (),
    blocking: tuple[PlanningReasonCode, ...] = (),
    starter_slots: tuple[StarterSlot, ...] = (StarterSlot(0, "G"), StarterSlot(1, "UTIL")),
) -> TeamWeekState:
    return TeamWeekState(
        league_id="league-1",
        season="2026",
        week=1,
        roster_id=1,
        decision_time=NOW,
        starter_slots=starter_slots,
        roster_player_ids=tuple(
            sorted({opportunity.sleeper_player_id for opportunity in opportunities})
        ),
        observed_starters=observed,
        opportunities=opportunities,
        fixed_slots=fixed,
        passed_opportunities=passed,
        scoring_policy_version="scoring-v1",
        league_configuration_version="league-v1",
        manager_policy_version="policy-v1",
        projection_model_version="fixture-model",
        input_version="fixture-inputs",
        freshness=FreshnessSummary((SourceLineage("fixture", "v1", NOW, NOW),)),
        blocking_reasons=blocking,
    )


def _assignment_players(decision) -> tuple[str | None, ...]:
    return tuple(assignment.player_id for assignment in decision.selected.assignments)


def test_equal_terminal_values_prefer_observed_placement_and_return_alternative() -> None:
    start = NOW + timedelta(hours=1)
    state = _state(
        (
            _opportunity("p1", "g1", start, ("PG",), ((10, 1),)),
            _opportunity("p2", "g1", start, ("PG",), ((10, 1),)),
        ),
        observed=(ObservedStarter(0, "p1", ("PG",)),),
        starter_slots=(StarterSlot(0, "G"),),
    )

    decision = score_weekly_options(state, config=WeeklyPlanPolicyConfig(scenario_count=5, seed=7))

    assert decision.approximation is TerminalValueApproximation.COMPLETE_ASSIGNMENT_ROLLOUT
    assert _assignment_players(decision) == ("p1",)
    assert decision.alternative is not None
    assert tuple(item.player_id for item in decision.alternative.assignments) == ("p2",)
    assert decision.selected.retained_observed_count == 1
    assert decision.selected.move_count == 0


def test_lower_standalone_value_preserves_a_scarce_future_opportunity() -> None:
    start = NOW + timedelta(hours=1)
    later = NOW + timedelta(days=1)
    state = _state(
        (
            _opportunity("p1", "g1", start, ("G",), ((10, 1),)),
            _opportunity("p2", "g1", start, ("G",), ((8, 1),)),
            _opportunity("p1", "g2", later, ("C",), ((50, 1),)),
        )
    )

    decision = score_weekly_options(state, config=WeeklyPlanPolicyConfig(scenario_count=5))

    assert decision.selected.assignments[0].player_id == "p2"
    assert (
        next(
            item for item in decision.evaluations if item.player_id == "p1"
        ).standalone_expected_value
        == 10
    )
    assert (
        next(
            item for item in decision.evaluations if item.player_id == "p2"
        ).standalone_expected_value
        == 8
    )
    assert decision.selected.expected_terminal_value <= decision.perfect_information_bound


def test_continuation_value_is_allocated_once_across_selected_assignment() -> None:
    start = NOW + timedelta(hours=1)
    later = NOW + timedelta(days=1)
    state = _state(
        (
            _opportunity("p1", "g1", start, ("PG",), ((10, 1),)),
            _opportunity("p2", "g2", start, ("C",), ((10, 1),)),
            _opportunity("p3", "g3", later, ("PG",), ((30, 1),)),
        )
    )

    decision = score_weekly_options(state, config=WeeklyPlanPolicyConfig(scenario_count=5))

    assert decision.baseline_terminal_value == 30
    assert decision.selected.expected_terminal_value == 40
    assert decision.perfect_information_bound == 40
    assert decision.selected.expected_terminal_value <= decision.perfect_information_bound
    assert tuple(item.player_id for item in decision.selected.assignments) == ("p1", None)
    assert {
        (evaluation.player_id, evaluation.slot_index, evaluation.marginal_terminal_value)
        for evaluation in decision.evaluations
    } == {("p1", 0, 10), ("p1", 1, 10), ("p2", 1, 10)}


def test_terminal_value_bound_holds_when_current_slot_conflicts_with_future_player() -> None:
    start = NOW + timedelta(hours=1)
    later = NOW + timedelta(days=1)
    state = _state(
        (
            _opportunity("p1", "g1", start, ("PG",), ((10, 1),)),
            _opportunity("p2", "g2", start, ("PG",), ((10, 1),)),
            _opportunity("p1", "g3", later, ("PG",), ((30, 1),)),
        ),
        starter_slots=(StarterSlot(0, "G"),),
    )

    decision = score_weekly_options(state, config=WeeklyPlanPolicyConfig(scenario_count=5))

    assert decision.perfect_information_bound == 30
    assert decision.selected.expected_terminal_value == 30
    assert decision.selected.expected_terminal_value <= decision.perfect_information_bound
    assert {
        (
            evaluation.player_id,
            evaluation.marginal_terminal_value,
            evaluation.expected_terminal_value,
        )
        for evaluation in decision.evaluations
    } == {("p1", -20, 10), ("p2", -20, 10)}


def test_complete_assignment_scoring_charges_joint_continuation_conflict_once() -> None:
    start = NOW + timedelta(hours=1)
    later = NOW + timedelta(days=1)
    current_p1 = replace(
        _opportunity("p1", "g1", start, ("PG",), ((60, 1),)),
        eligible_slot_indices=(1,),
    )
    current_p2 = replace(
        _opportunity("p2", "g2", start, ("PG",), ((60, 1),)),
        eligible_slot_indices=(0,),
    )
    future_p1 = replace(
        _opportunity("p1", "g3", later, ("PG",), ((100, 1),)),
        eligible_slot_indices=(0,),
    )
    state = _state(
        (current_p1, current_p2, future_p1),
        starter_slots=(StarterSlot(0, "G"), StarterSlot(1, "UTIL")),
    )

    decision = score_weekly_options(state, config=WeeklyPlanPolicyConfig(scenario_count=5))

    assert tuple(item.player_id for item in decision.selected.assignments) == ("p2", "p1")
    assert decision.selected.expected_terminal_value == 120
    assert decision.perfect_information_bound == 120
    assert decision.alternative is not None
    assert decision.alternative.expected_terminal_value == 100


def test_best_alternative_includes_leaving_the_lineup_empty() -> None:
    start = NOW + timedelta(hours=1)
    state = _state(
        (_opportunity("p1", "g1", start, ("PG",), ((10, 1),)),),
        starter_slots=(StarterSlot(0, "G"),),
    )

    decision = score_weekly_options(state, config=WeeklyPlanPolicyConfig(scenario_count=5))

    assert decision.alternative is not None
    assert decision.alternative.assignments == (SlotAssignment(0, "G", None, None, None, 0),)
    assert decision.alternative.expected_terminal_value == 0


def test_fixed_slots_and_passed_opportunities_constrain_terminal_options() -> None:
    start = NOW + timedelta(hours=1)
    later = NOW + timedelta(days=1)
    current = _opportunity("p1", "g1", start, ("PG",), ((20, 1),))
    passed = _opportunity("p2", "g2", later, ("PG",), ((100, 1),))
    future = _opportunity("p3", "g3", later, ("C",), ((12, 1),))
    fixed = FixedSlot(0, "G", "p4", "g0", 20, NOW, "lock-1", "fixture")
    fixed_opportunity = _opportunity(
        "p4",
        "g0",
        NOW - timedelta(hours=2),
        ("PG",),
        ((20, 1),),
        status=PlanningGameStatus.FINAL,
        completed_score=20,
        finalized_at=NOW - timedelta(hours=1),
    )
    state = _state(
        (current, passed, future, fixed_opportunity),
        fixed=(fixed,),
        passed=(PassedOpportunity("p2", "g2", NOW, "pass-1", "fixture"),),
    )

    decision = score_weekly_options(state, config=WeeklyPlanPolicyConfig(scenario_count=5))

    assert decision.selected.assignments[0].slot_index == 1
    assert decision.selected.assignments[0].player_id == "p1"
    assert all(evaluation.player_id != "p2" for evaluation in decision.evaluations)
    assert decision.selected.expected_terminal_value >= 20


def test_simultaneous_tipoff_batch_and_uncertainty_are_deterministic() -> None:
    start = NOW + timedelta(hours=1)
    later = NOW + timedelta(days=1)
    state = _state(
        (
            _opportunity("p1", "g1", start, ("PG",), ((-5, 1), (15, 1))),
            _opportunity("p2", "g2", start, ("C",), ((0, 1), (20, 1))),
            _opportunity("p3", "g3", later, ("PG",), ((30, 1),)),
        )
    )
    config = WeeklyPlanPolicyConfig(scenario_count=50, seed=11)

    first = score_weekly_options(state, config=config)
    second = score_weekly_options(state, config=config)

    assert first == second
    assert first.batch_game_ids == ("g1", "g2")
    assert len(first.evaluations) == 3
    assert {evaluation.game_id for evaluation in first.evaluations} == {"g1", "g2"}
    assert first.alternative is not None


def test_blocked_state_fails_closed_with_reason() -> None:
    start = NOW + timedelta(hours=1)
    state = _state(
        (_opportunity("p1", "g1", start, ("PG",), ((10, 1),)),),
        blocking=(PlanningReasonCode.MISSING_PROJECTION,),
    )

    with pytest.raises(WeeklyPlanError, match="missing_projection"):
        score_weekly_options(state)


def test_future_realized_outcomes_do_not_change_an_earlier_policy() -> None:
    current_start = NOW + timedelta(hours=1)
    future_start = NOW + timedelta(days=1)
    games = (
        ReplayGame(
            "g1",
            current_start,
            current_start + timedelta(hours=2),
            1,
            ("home", "away"),
            ReplayGameStatus.FINAL,
        ),
        ReplayGame(
            "g2",
            future_start,
            future_start + timedelta(hours=2),
            1,
            ("home", "away"),
            ReplayGameStatus.FINAL,
        ),
    )
    projection_1 = _projection("p1", "g1", ((10, 1),))
    projection_2 = _projection("p2", "g2", ((12, 1),))
    player_games = (
        ReplayPlayerGame("p1", "provider-p1", "g1", 1, True, ("PG",), 10, projection_1),
        ReplayPlayerGame("p2", "provider-p2", "g2", 1, True, ("PG",), 12, projection_2),
    )
    changed_player_games = replace(player_games[1], actual_score=900)
    config = ReplayConfig(starter_slots=("G",), league_id="league-1", week=1, roster_id=1)
    first_state = team_week_state_from_replay(
        ReplayState(("G",), games, player_games),
        config=config,
        decision_time=NOW,
    )
    second_state = team_week_state_from_replay(
        ReplayState(("G",), games, (player_games[0], changed_player_games)),
        config=config,
        decision_time=NOW,
    )

    assert score_weekly_options(first_state, config=WeeklyPlanPolicyConfig(scenario_count=5)) == (
        score_weekly_options(second_state, config=WeeklyPlanPolicyConfig(scenario_count=5))
    )
