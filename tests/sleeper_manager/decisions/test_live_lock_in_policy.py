"""Live Lock-In evaluation coverage over deterministic policy scenarios."""

from datetime import UTC, datetime, timedelta

from sleeper_manager.decisions.live_lock_in import (
    LiveLockInPolicyConfig,
    evaluate_live_lock_in,
)
from sleeper_manager.decisions.lock_in import LockInPolicyConfig, ScoreMaximizingLockInPolicy
from sleeper_manager.domain.lock_in import LockInEvaluationKind
from sleeper_manager.domain.planning import (
    GameOpportunity,
    PlanningGameStatus,
    PlanningQuality,
    StarterSlot,
    TeamWeekState,
)
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot

NOW = datetime(2026, 1, 10, 18, tzinfo=UTC)


def _opportunity(
    game_id: str,
    expected: float,
    *,
    actual: float | None = None,
    start_offset: int = 1,
) -> GameOpportunity:
    """Build one finalized or scheduled opportunity for the same player."""

    projection = ProjectionSnapshot(
        player_id="player",
        game_id=game_id,
        available_as_of=NOW - timedelta(hours=1),
        model_version="fixture",
        input_version=f"projection-{game_id}",
        scoring_policy_version="scoring-v1",
        distribution=ProjectionDistribution.from_weighted_observations(((expected, 1.0),)),
        reasons=(),
    )
    finalized = actual is not None
    return GameOpportunity(
        sleeper_player_id="player",
        provider_player_id="espn-player",
        game_id=game_id,
        scheduled_start=NOW + timedelta(hours=start_offset),
        status=PlanningGameStatus.FINAL if finalized else PlanningGameStatus.SCHEDULED,
        roster_id=1,
        membership_segment="segment",
        eligible_slot_indices=(0,),
        eligible_positions=("PG",),
        rostered_at_tipoff=True,
        availability_status="available",
        availability_evidence_at=NOW - timedelta(hours=1),
        projection=projection,
        missing_projection_reason=None,
        completed_fantasy_score=actual,
        finalized_at=NOW - timedelta(minutes=1) if finalized else None,
    )


def _state(opportunities: tuple[GameOpportunity, ...]) -> TeamWeekState:
    """Build exact team-week evidence for live policy evaluation."""

    return TeamWeekState(
        league_id="league",
        season="2026",
        week=1,
        roster_id=1,
        decision_time=NOW,
        starter_slots=(StarterSlot(0, "PG"),),
        roster_player_ids=("player",),
        observed_starters=(),
        opportunities=opportunities,
        fixed_slots=(),
        passed_opportunities=(),
        scoring_policy_version="scoring-v1",
        league_configuration_version="league-v1",
        manager_policy_version="manager-v1",
        projection_model_version="fixture",
        input_version="inputs-v1",
        eligibility_quality=PlanningQuality.EXACT,
    )


def _evaluate(
    completed: GameOpportunity,
    future: GameOpportunity | None,
    *,
    minimum_confidence: float = 0.7,
    deadline: datetime | None = None,
):
    """Evaluate one fixture with the deterministic shared policy."""

    opportunities = (completed,) if future is None else (completed, future)
    return evaluate_live_lock_in(
        _state(opportunities),
        completed,
        deadline=deadline or NOW + timedelta(hours=6),
        manager_policy_version="manager-v1",
        policy=ScoreMaximizingLockInPolicy(
            LockInPolicyConfig(scenario_count=20, seed=7, tie_tolerance=0.01)
        ),
        config=LiveLockInPolicyConfig(minimum_confidence),
    )


def test_live_policy_reports_deterministic_lock_confidence() -> None:
    """Count material scenario wins for a clearly superior completed score."""

    result = _evaluate(
        _opportunity("completed", 20, actual=20, start_offset=-3),
        _opportunity("future", 1),
    )

    assert result.kind is LockInEvaluationKind.LOCK
    assert result.confidence == 1.0
    assert result.alternative_percentiles == ((10, 1.0), (50, 1.0), (90, 1.0))


def test_live_policy_waits_on_exact_ties() -> None:
    """Keep a mean-best tie silent when no scenario beats the tolerance."""

    result = _evaluate(
        _opportunity("completed", 10, actual=10, start_offset=-3),
        _opportunity("future", 10),
    )

    assert result.kind is LockInEvaluationKind.WAIT
    assert result.confidence == 0.0
    assert result.decision is None
    assert result.reason_codes == ("confidence_below_threshold",)


def test_live_policy_marks_final_eligible_game_automatic() -> None:
    """Avoid unnecessary manager advice when no later player-game exists."""

    result = _evaluate(
        _opportunity("completed", 15, actual=15, start_offset=-3),
        None,
    )

    assert result.kind is LockInEvaluationKind.AUTOMATIC_FINAL
    assert result.observed_score == 15
    assert result.decision is None


def test_live_policy_refuses_advice_after_deadline() -> None:
    """Return unavailable instead of producing a stale actionable decision."""

    result = _evaluate(
        _opportunity("completed", 20, actual=20, start_offset=-3),
        _opportunity("future", 1),
        deadline=NOW,
    )

    assert result.kind is LockInEvaluationKind.UNAVAILABLE
    assert result.reason_codes == ("deadline_elapsed",)
