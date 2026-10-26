"""Full recommendations coordinate score actions with current observations and recovery."""

from dataclasses import replace
from datetime import timedelta

from sleeper_manager.decisions.team_recommendation import build_team_recommendation
from sleeper_manager.domain.lock_in import LockInDecisionKind
from sleeper_manager.domain.planning import ObservedStarter, PlanningGameStatus
from sleeper_manager.domain.projection import ProjectionDistribution
from tests.sleeper_manager.decisions.test_recoverable_attempts import POLICY, _trial
from tests.sleeper_manager.decisions.weekly_plan_support import NOW


def _after_trial(score):
    state = replace(_trial(), week_ends_at=NOW + timedelta(days=6))
    first = build_team_recommendation(state, policy=POLICY)
    current = NOW + timedelta(hours=23)
    early = replace(
        state.opportunities[0],
        status=PlanningGameStatus.FINAL,
        completed_fantasy_score=score,
        finalized_at=current - timedelta(hours=20),
    )
    return replace(
        state,
        decision_time=current,
        observed_starters=(ObservedStarter(0, "early", ("PG",)),),
        opportunities=(early, state.opportunities[1]),
        recovery_obligations=first.recovery_obligations,
    )


def test_weak_completed_trial_proposes_pass_and_restores_later_player() -> None:
    plan = build_team_recommendation(_after_trial(5), policy=POLICY)
    assert plan.score_actions[0].kind is LockInDecisionKind.PASS
    assert plan.desired_assignments[0].player_id == "later"
    assert plan.expected_terminal_score == 60
    assert plan.fixed_slots == ()
    assert plan.passed_opportunities == ()
    assert plan.forfeited_opportunities == (("early", "tonight"),)


def test_strong_completed_trial_is_a_proposal_until_confirmed() -> None:
    state = _after_trial(80)
    first = build_team_recommendation(state, policy=POLICY)
    second = build_team_recommendation(state, policy=POLICY)
    assert first.score_actions[0].kind is LockInDecisionKind.LOCK
    assert first.score_actions[0].slot_index == 0
    assert first.fixed_slots == second.fixed_slots == ()
    assert first.expected_terminal_score == 80
    assert first.material_hash == second.material_hash
    assert first.bench_player_ids == ("later",)


def test_active_trial_can_be_discarded_at_recovery_without_inventing_final_evidence() -> None:
    state = _after_trial(5)
    active = replace(
        state.opportunities[0],
        status=PlanningGameStatus.ACTIVE,
        completed_fantasy_score=None,
        finalized_at=None,
    )
    plan = build_team_recommendation(
        replace(
            state,
            decision_time=state.opportunities[1].scheduled_start - timedelta(minutes=15),
            opportunities=(active, state.opportunities[1]),
        ),
        policy=POLICY,
    )
    assert plan.score_actions == ()
    assert plan.forfeited_opportunities == (("early", "tonight"),)
    assert plan.desired_assignments[0].player_id == "later"


def test_ignoring_the_trial_recommendation_does_not_create_a_recovery_action() -> None:
    state = replace(_trial(), week_ends_at=NOW + timedelta(days=6))
    first = build_team_recommendation(state, policy=POLICY)
    current = replace(
        state,
        decision_time=NOW + timedelta(hours=23),
        recovery_obligations=first.recovery_obligations,
        opportunities=(
            replace(state.opportunities[0], status=PlanningGameStatus.ACTIVE),
            state.opportunities[1],
        ),
    )
    result = build_team_recommendation(current, policy=POLICY)
    assert result.recovery_obligations == ()
    assert result.forfeited_opportunities == ()
    assert result.desired_assignments[0].player_id == "later"


def test_negative_final_trial_can_be_discarded_before_week_end() -> None:
    state = _trial()
    state = replace(
        state,
        opportunities=(
            replace(
                state.opportunities[0],
                projection=replace(
                    state.opportunities[0].projection,
                    distribution=ProjectionDistribution.from_weighted_observations(((-5, 1),)),
                ),
            ),
        ),
        week_ends_at=NOW + timedelta(days=1),
        roster_eligibility=(("early", ("PG",)), ("later", ("PG",))),
    )
    first = build_team_recommendation(state, policy=POLICY)
    assert first.recovery_obligations
    at = state.week_ends_at - timedelta(minutes=5)
    final = replace(
        state.opportunities[0],
        status=PlanningGameStatus.FINAL,
        completed_fantasy_score=-5,
        finalized_at=at - timedelta(hours=1),
    )
    current = replace(
        state,
        decision_time=at,
        observed_starters=(ObservedStarter(0, "early", ("PG",)),),
        opportunities=(final,),
        recovery_obligations=first.recovery_obligations,
    )
    result = build_team_recommendation(current, policy=POLICY)
    assert result.fixed_slots == ()
    assert result.score_actions[0].kind is LockInDecisionKind.PASS
    assert result.desired_assignments[0].player_id == "later"


def test_known_score_and_next_start_use_the_same_bounded_continuation() -> None:
    from sleeper_manager.domain.planning import StarterSlot
    from tests.sleeper_manager.decisions.weekly_plan_support import _opportunity, _state

    known = _opportunity(
        "held",
        "done",
        NOW - timedelta(hours=3),
        ("PG",),
        ((85, 1),),
        status=PlanningGameStatus.FINAL,
        completed_score=85,
        finalized_at=NOW - timedelta(hours=1),
        eligible_slot_indices=(0,),
    )
    early = _opportunity(
        "trial", "next", NOW + timedelta(hours=1), ("PG",), ((80, 1),), eligible_slot_indices=(0,)
    )
    later = _opportunity(
        "held",
        "later",
        NOW + timedelta(days=1),
        ("PG",),
        ((60, 1), (100, 1)),
        eligible_slot_indices=(0,),
    )
    state = replace(
        _state(
            (known, early, later),
            starter_slots=(StarterSlot(0, "UTIL"),),
            observed=(ObservedStarter(0, "held", ("PG",)),),
        ),
        bench_capacity=1,
        week_ends_at=NOW + timedelta(days=6),
    )
    policy = replace(
        POLICY, scenario_count=512, postgame_scenario_count=128, minimum_lock_in_confidence=0.4
    )
    result = build_team_recommendation(state, policy=policy)
    assert result.score_actions[0].kind is LockInDecisionKind.LOCK
    assert result.score_actions[0].player_id == "held"
    assert result.expected_terminal_score == 85
    assert result.desired_assignments[0].player_id == "held"


def test_move_search_failure_returns_a_full_blocked_view(monkeypatch) -> None:
    from sleeper_manager.decisions import team_recommendation
    from sleeper_manager.decisions.weekly_plan import WeeklyPlanError
    from sleeper_manager.domain.planning import PlanningReasonCode, PlanStatus

    def fail(*args, **kwargs):
        raise WeeklyPlanError("No verified legal lineup sequence within search limit")

    monkeypatch.setattr(team_recommendation, "coordinate_completed_continuations", fail)
    state = _after_trial(80)
    result = build_team_recommendation(state, policy=POLICY)
    assert result.status is PlanStatus.BLOCKED
    assert result.blocking_reasons == (PlanningReasonCode.SEARCH_LIMIT,)
    assert result.moves == result.score_actions == ()
    assert result.desired_assignments == result.observed_assignments
