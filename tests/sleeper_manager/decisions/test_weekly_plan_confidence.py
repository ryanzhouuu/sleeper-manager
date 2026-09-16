"""Static-plan evidence and confidence caps for weekly plans."""

from datetime import timedelta

from sleeper_manager.decisions.weekly_plan import (
    build_weekly_plan,
)
from sleeper_manager.domain.planning import (
    FixedSlot,
    ObservedStarter,
    PassedOpportunity,
    PlanConfidence,
    PlanningGameStatus,
    PlanningQuality,
    PlanningReasonCode,
    PlanStatus,
    StarterSlot,
)
from tests.sleeper_manager.decisions.weekly_plan_support import (
    NOW,
    _opportunity,
    _planned,
    _state,
)


def test_static_plan_preserves_fixed_slots_and_orders_passed_evidence() -> None:
    """Keep accepted results and audit evidence stable after the week is exhausted."""
    finalized_at = NOW - timedelta(hours=1)
    fixed_opportunity = _opportunity(
        "p3",
        "g0",
        NOW - timedelta(hours=2),
        ("PG",),
        ((20, 1),),
        status=PlanningGameStatus.FINAL,
        completed_score=20,
        finalized_at=finalized_at,
        eligible_slot_indices=(0,),
    )
    passed_p1 = _opportunity("p1", "g1", NOW + timedelta(hours=1), ("PG",), ((10, 1),))
    passed_p2 = _opportunity("p2", "g2", NOW + timedelta(hours=2), ("PG",), ((12, 1),))
    state = _state(
        (fixed_opportunity, passed_p2, passed_p1),
        observed=(ObservedStarter(0, "p3", ("PG",)),),
        fixed=(FixedSlot(0, "G", "p3", "g0", 20, NOW, "lock-1", "fixture"),),
        passed=(
            PassedOpportunity("p2", "g2", NOW, "pass-2", "fixture"),
            PassedOpportunity("p1", "g1", NOW, "pass-1", "fixture"),
        ),
    )

    plan = build_weekly_plan(state)

    assert plan.status is PlanStatus.NO_ACTION
    assert plan.desired_assignments == (
        _planned(0, "G", "p3"),
        _planned(1, "UTIL", None),
    )
    assert tuple((item.player_id, item.game_id) for item in plan.passed_opportunities) == (
        ("p1", "g1"),
        ("p2", "g2"),
    )


def test_build_weekly_plan_labels_warned_plans_degraded() -> None:
    start = NOW + timedelta(hours=1)
    state = _state(
        (
            _opportunity("p1", "g1", start, ("PG",), ((30, 1),)),
            _opportunity("p2", "g1", start, ("PG",), ((10, 1),)),
        ),
        observed=(ObservedStarter(0, "p1", ("PG",)),),
        starter_slots=(StarterSlot(0, "G"),),
        quality=PlanningQuality.EXACT,
        warnings=(PlanningReasonCode.STALE_NBA_STATE,),
    )

    plan = build_weekly_plan(state, lead_time=timedelta(minutes=10))

    assert plan.status is PlanStatus.DEGRADED
    assert plan.moves == ()
    assert plan.warnings == (PlanningReasonCode.STALE_NBA_STATE,)
    assert plan.confidence is PlanConfidence.HIGH


def test_high_confidence_requires_exact_quality_or_a_clear_margin() -> None:
    start = NOW + timedelta(hours=1)
    state = _state(
        (
            _opportunity("p1", "g1", start, ("PG",), ((10, 1),)),
            _opportunity("p2", "g1", start, ("PG",), ((30, 1),)),
        ),
        observed=(ObservedStarter(0, "p1", ("PG",)),),
        quality=PlanningQuality.EXACT,
        starter_slots=(StarterSlot(0, "G"),),
    )

    plan = build_weekly_plan(state)

    assert plan.status is PlanStatus.ACTION_REQUIRED
    assert plan.decision_margin == 20
    assert plan.confidence is PlanConfidence.HIGH


def test_best_known_constraints_quality_caps_confidence_at_medium() -> None:
    """Avoid high confidence when eligibility came from the constraints oracle."""
    start = NOW + timedelta(hours=1)
    state = _state(
        (
            _opportunity("p1", "g1", start, ("PG",), ((10, 1),)),
            _opportunity("p2", "g1", start, ("PG",), ((30, 1),)),
        ),
        observed=(ObservedStarter(0, "p1", ("PG",)),),
        quality=PlanningQuality.BEST_KNOWN_CONSTRAINTS_ORACLE,
        starter_slots=(StarterSlot(0, "G"),),
    )

    plan = build_weekly_plan(state)

    assert plan.decision_margin == 20
    assert plan.confidence is PlanConfidence.MEDIUM
