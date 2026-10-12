"""Do not recommend a lock that would bench an active starter."""

from dataclasses import replace
from datetime import timedelta

from sleeper_manager.decisions._lock_in_placements import feasible_lock_slot_indices
from sleeper_manager.decisions.live_lock_in import LiveLockInPolicyConfig, evaluate_live_lock_in
from sleeper_manager.decisions.lock_in import LockInPolicyConfig, ScoreMaximizingLockInPolicy
from sleeper_manager.domain.lock_in import LockInEvaluationKind
from sleeper_manager.domain.planning import ObservedStarter, PlanningGameStatus, StarterSlot

from .weekly_plan_support import NOW, _opportunity, _state


def _inputs(slots):
    active = _opportunity(
        "active",
        "ongoing",
        NOW - timedelta(hours=1),
        ("PG",),
        ((10, 1),),
        status=PlanningGameStatus.ACTIVE,
        eligible_slot_indices=(0,),
    )
    completed = _opportunity(
        "done",
        "done",
        NOW - timedelta(hours=3),
        ("PG", "C"),
        ((50, 1),),
        status=PlanningGameStatus.FINAL,
        completed_score=50,
        finalized_at=NOW - timedelta(minutes=1),
    )
    later = _opportunity("done", "later", NOW + timedelta(days=1), ("PG", "C"), ((10, 1),))
    state = _state(
        (active, completed, later),
        observed=(ObservedStarter(0, "active", ("PG",)),),
        starter_slots=slots,
    )
    return state, completed


def test_only_a_lock_preserving_scarce_active_eligibility_is_feasible() -> None:
    state, completed = _inputs((StarterSlot(0, "G"), StarterSlot(1, "UTIL")))
    assert feasible_lock_slot_indices(state, completed) == {1}
    decision = ScoreMaximizingLockInPolicy(LockInPolicyConfig(scenario_count=8)).decide_after_game(
        state, completed
    )
    assert decision.slot_index == 1


def test_live_policy_waits_until_active_capacity_is_released() -> None:
    state, completed = _inputs((StarterSlot(0, "G"),))
    completed = replace(completed, eligible_slot_indices=(0,))
    evaluation = evaluate_live_lock_in(
        state,
        completed,
        deadline=NOW + timedelta(hours=1),
        manager_policy_version="policy",
        policy=ScoreMaximizingLockInPolicy(LockInPolicyConfig(scenario_count=8)),
        config=LiveLockInPolicyConfig(0.7),
    )
    assert evaluation.kind is LockInEvaluationKind.WAIT
    assert evaluation.reason_codes == ("active_starters_block_lock",)
