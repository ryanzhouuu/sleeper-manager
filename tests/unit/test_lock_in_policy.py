"""Policy coverage for the production-neutral Lock-In planning boundary."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sleeper_manager.decisions.lock_in import (
    LockInPolicyConfig,
    LockInPolicyError,
    ScoreMaximizingLockInPolicy,
    decision_critical_opportunities,
    missing_decision_projection_keys,
)
from sleeper_manager.domain.lock_in import LockInDecisionKind
from sleeper_manager.domain.planning import (
    FixedSlot,
    GameOpportunity,
    PassedOpportunity,
    PlanningGameStatus,
    PlanningQuality,
    PlanningReasonCode,
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
    projection: bool = True,
) -> GameOpportunity:
    """Build a finalized or future opportunity with exact slot eligibility."""

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
        projection=_projection(player_id, game_id, expected) if projection else None,
        missing_projection_reason=None if projection else PlanningReasonCode.MISSING_PROJECTION,
        completed_fantasy_score=actual,
        finalized_at=NOW - timedelta(minutes=1) if finalized else None,
    )


def _state(
    opportunities: tuple[GameOpportunity, ...],
    *,
    slots: tuple[StarterSlot, ...] = (StarterSlot(0, "PG"),),
    fixed_slots: tuple[FixedSlot, ...] = (),
    passed: tuple[PassedOpportunity, ...] = (),
    blocking: tuple[PlanningReasonCode, ...] = (),
) -> TeamWeekState:
    """Build validated team-week state around supplied policy evidence."""

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
        observed_starters=(),
        opportunities=opportunities,
        fixed_slots=fixed_slots,
        passed_opportunities=passed,
        scoring_policy_version="scoring-v1",
        league_configuration_version="league-v1",
        manager_policy_version="manager-v1",
        projection_model_version="fixture",
        input_version="team-week-inputs-v1",
        eligibility_quality=PlanningQuality.EXACT,
        blocking_reasons=blocking,
    )


def test_policy_locks_known_high_score_and_passes_for_future_upside() -> None:
    """Preserve the existing deterministic Lock/Pass comparison behavior."""

    policy = ScoreMaximizingLockInPolicy(LockInPolicyConfig(scenario_count=20, seed=7))
    completed = _opportunity("p1", "g1", 10, actual=10, start_offset=-3)
    low_state = _state((completed, _opportunity("p2", "g2", 1)))
    high_state = _state((completed, _opportunity("p2", "g3", 20)))

    lock = policy.decide_after_game(low_state, completed)
    passing = policy.decide_after_game(high_state, completed)

    assert lock.kind is LockInDecisionKind.LOCK
    assert passing.kind is LockInDecisionKind.PASS
    assert lock.information_version == "team-week-inputs-v1"


def test_policy_comparison_exposes_paired_scenarios_without_changing_decision() -> None:
    """Expose scenario evidence while retaining the historical policy result."""

    policy = ScoreMaximizingLockInPolicy(LockInPolicyConfig(scenario_count=20, seed=7))
    completed = _opportunity("p1", "g1", 10, actual=10, start_offset=-3)
    state = _state((completed, _opportunity("p1", "g2", 1)))

    comparison = policy.compare_after_game(state, completed)

    assert comparison.decision == policy.decide_after_game(state, completed)
    assert comparison.selected_terminal_scores == (10.0,) * 20
    assert comparison.counterfactual_terminal_scores == (1.0,) * 20


def test_policy_honors_exact_slot_indices_with_duplicate_positions() -> None:
    """Never place a completed score into a same-label slot it could not occupy."""

    completed = _opportunity(
        "p1",
        "g1",
        30,
        actual=30,
        slot_indices=(1,),
        start_offset=-3,
    )
    state = _state(
        (completed, _opportunity("p2", "g2", 1, slot_indices=(0, 1))),
        slots=(StarterSlot(0, "UTIL"), StarterSlot(1, "UTIL")),
    )

    decision = ScoreMaximizingLockInPolicy(LockInPolicyConfig(scenario_count=16)).decide_after_game(
        state, completed
    )

    assert decision.kind is LockInDecisionKind.LOCK
    assert decision.slot_index == 1


def test_remaining_opportunities_exclude_fixed_passed_and_unusable_games() -> None:
    """Share one complete opportunity selector across policy and diagnostics."""

    completed = _opportunity(
        "candidate",
        "candidate-game",
        20,
        actual=20,
        slot_indices=(1, 2),
        start_offset=-3,
    )
    locked = _opportunity(
        "locked",
        "locked-game",
        15,
        actual=15,
        slot_indices=(0,),
        start_offset=-4,
    )
    usable = _opportunity("usable", "usable-game", 10, slot_indices=(2,))
    passed = _opportunity("passed", "passed-game", 12, slot_indices=(1,))
    closed_slot = _opportunity("closed", "closed-game", 50, slot_indices=(0,))
    fixed = FixedSlot(
        0,
        "UTIL",
        "locked",
        "locked-game",
        15,
        NOW - timedelta(minutes=2),
        "lock-1",
        "fixture",
    )
    passed_record = PassedOpportunity(
        "passed",
        "passed-game",
        NOW - timedelta(minutes=2),
        "pass-1",
        "fixture",
    )
    state = _state(
        (completed, locked, usable, passed, closed_slot),
        slots=(
            StarterSlot(0, "UTIL"),
            StarterSlot(1, "UTIL"),
            StarterSlot(2, "UTIL"),
        ),
        fixed_slots=(fixed,),
        passed=(passed_record,),
    )

    remaining = decision_critical_opportunities(state, completed)

    assert tuple(item.sleeper_player_id for item in remaining) == ("usable",)


def test_missing_projection_is_visible_and_blocks_policy_execution() -> None:
    """Defer incomplete evidence instead of evaluating a reduced future set."""

    completed = _opportunity("p1", "g1", 20, actual=20, start_offset=-3)
    missing = _opportunity("p2", "g2", 10, projection=False)
    state = _state(
        (completed, missing),
        blocking=(PlanningReasonCode.MISSING_PROJECTION,),
    )
    remaining = decision_critical_opportunities(state, completed)

    assert missing_decision_projection_keys(remaining) == ("p2:g2",)
    with pytest.raises(LockInPolicyError, match="missing_projection"):
        ScoreMaximizingLockInPolicy().decide_after_game(state, completed)


def test_decision_modules_do_not_import_backtesting() -> None:
    """Protect the production policy boundary from replay ownership regressions."""

    source_root = Path(__file__).parents[2] / "src" / "sleeper_manager" / "decisions"

    for name in ("lock_in.py", "simulation.py"):
        assert "sleeper_manager.backtesting" not in (source_root / name).read_text()
