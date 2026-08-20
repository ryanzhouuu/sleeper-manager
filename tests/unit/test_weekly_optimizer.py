from datetime import UTC, datetime, timedelta

from sleeper_manager.backtesting.replay import (
    ReplayConfig,
    compare_team_week,
    oracle_team_week_result,
)
from sleeper_manager.backtesting.replay.models import (
    ReplayGame,
    ReplayGameStatus,
    ReplayPlayerGame,
    TeamWeekReplayResult,
)
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig, score_weekly_options
from sleeper_manager.domain.eligibility import eligible_for_slot
from sleeper_manager.domain.planning import (
    FreshnessSummary,
    GameOpportunity,
    PlanningGameStatus,
    SourceLineage,
    StarterSlot,
    TeamWeekState,
)
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot

NOW = datetime(2026, 1, 5, 12, tzinfo=UTC)


def test_oracle_selects_realized_best_game_and_comparison_handles_zero_denominator() -> None:
    games = (
        ReplayGame(
            "g1",
            datetime(2026, 1, 5, 1, tzinfo=UTC),
            datetime(2026, 1, 5, 3, tzinfo=UTC),
            1,
            ("a", "b"),
            ReplayGameStatus.FINAL,
        ),
        ReplayGame(
            "g2",
            datetime(2026, 1, 7, 1, tzinfo=UTC),
            datetime(2026, 1, 7, 3, tzinfo=UTC),
            1,
            ("a", "b"),
            ReplayGameStatus.FINAL,
        ),
    )
    player_games = (
        ReplayPlayerGame("p1", "provider-p1", "g1", 1, True, ("PG",), 12),
        ReplayPlayerGame("p1", "provider-p1", "g2", 1, True, ("PG",), 8),
    )
    oracle = oracle_team_week_result(
        player_games,
        games=games,
        config=ReplayConfig(starter_slots=("PG",), league_id="league", roster_id=1),
    )
    assert oracle.realized_score == 12
    assert oracle.locked_slots[0].game_id == "g1"

    zero = TeamWeekReplayResult("league", 1, 1, "oracle", 0, (), (), (), "exact", "complete")
    comparison = compare_team_week(zero, zero)
    assert comparison.lock_in_regret == 0
    assert comparison.score_capture is None


def test_weekly_optimizer_outputs_only_legal_slot_player_and_game_assignments() -> None:
    start = NOW + timedelta(hours=1)
    later = NOW + timedelta(days=1)
    opportunities = (
        _opportunity("p1", "g1", start, ("PG",), 10),
        _opportunity("p2", "g2", start, ("C",), 8),
        _opportunity("p3", "g3", later, ("PG",), 30),
    )
    state = TeamWeekState(
        league_id="league-1",
        season="2026",
        week=1,
        roster_id=1,
        decision_time=NOW,
        starter_slots=(StarterSlot(0, "G"), StarterSlot(1, "UTIL")),
        roster_player_ids=("p1", "p2", "p3"),
        observed_starters=(),
        opportunities=opportunities,
        fixed_slots=(),
        passed_opportunities=(),
        scoring_policy_version="scoring-v1",
        league_configuration_version="league-v1",
        manager_policy_version="policy-v1",
        projection_model_version="fixture-model",
        input_version="fixture-inputs",
        freshness=FreshnessSummary((SourceLineage("fixture", "v1", NOW, NOW),)),
        blocking_reasons=(),
    )

    decision = score_weekly_options(state, config=WeeklyPlanPolicyConfig(scenario_count=5))
    by_id = {(item.sleeper_player_id, item.game_id): item for item in opportunities}

    for evaluation in decision.evaluations:
        opportunity = by_id[(evaluation.player_id, evaluation.game_id)]
        assert evaluation.slot_index in opportunity.eligible_slot_indices
        assert evaluation.slot_position in opportunity.eligible_positions or eligible_for_slot(
            opportunity.eligible_positions, evaluation.slot_position
        )

    assigned_players: set[str] = set()
    for assignment in decision.selected.assignments:
        if assignment.player_id is None:
            assert assignment.candidate_id is None
            assert assignment.game_id is None
            continue
        assert assignment.player_id not in assigned_players
        assigned_players.add(assignment.player_id)
        opportunity = by_id[(assignment.player_id, assignment.game_id)]
        assert assignment.slot_index in opportunity.eligible_slot_indices
        assert assignment.candidate_id == (
            f"{assignment.player_id}:{assignment.game_id}:{opportunity.membership_segment}"
            f"@slot-{assignment.slot_index}"
        )


def _opportunity(
    player_id: str,
    game_id: str,
    scheduled_start: datetime,
    positions: tuple[str, ...],
    projection_value: float,
) -> GameOpportunity:
    return GameOpportunity(
        sleeper_player_id=player_id,
        provider_player_id=f"provider-{player_id}",
        game_id=game_id,
        scheduled_start=scheduled_start,
        status=PlanningGameStatus.SCHEDULED,
        roster_id=1,
        membership_segment="segment-1",
        eligible_slot_indices=tuple(
            index for index, slot in enumerate(("G", "UTIL")) if eligible_for_slot(positions, slot)
        ),
        eligible_positions=positions,
        rostered_at_tipoff=True,
        availability_status="available",
        availability_evidence_at=NOW,
        projection=ProjectionSnapshot(
            player_id=player_id,
            game_id=game_id,
            available_as_of=NOW,
            model_version="fixture-model",
            input_version="fixture-inputs",
            scoring_policy_version="fixture-scoring",
            distribution=ProjectionDistribution.from_weighted_observations(
                ((projection_value, 1),)
            ),
            reasons=(),
        ),
        missing_projection_reason=None,
        completed_fantasy_score=None,
        finalized_at=None,
        source_lineage=(SourceLineage("fixture", "v1", NOW, NOW),),
    )
