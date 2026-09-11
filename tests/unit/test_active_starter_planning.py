"""Regression coverage for active starters in chronological weekly planning."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

import sleeper_manager.decisions._weekly_plan_scoring as scoring_module
from sleeper_manager.backtesting.experiments.full_advisor_replay_legality import (
    active_target_slots,
)
from sleeper_manager.backtesting.experiments.full_advisor_replay_models import (
    FullAdvisorReplayError,
)
from sleeper_manager.decisions.weekly_plan import (
    WeeklyPlanPolicyConfig,
    build_weekly_plan,
    score_weekly_options,
)
from sleeper_manager.domain.planning import (
    FixedSlot,
    GameOpportunity,
    ObservedStarter,
    PlanningGameStatus,
    StarterSlot,
    TeamWeekState,
)
from sleeper_manager.domain.projection import ProjectionDistribution, ProjectionSnapshot

NOW = datetime(2026, 1, 5, 12, tzinfo=UTC)


def _opportunity(
    player_id: str,
    game_id: str,
    *,
    start: datetime,
    status: PlanningGameStatus,
    eligible_slot_indices: tuple[int, ...],
    expected: float,
) -> GameOpportunity:
    """Build one projected player-game with explicit slot eligibility."""

    return GameOpportunity(
        sleeper_player_id=player_id,
        provider_player_id=f"provider-{player_id}",
        game_id=game_id,
        scheduled_start=start,
        status=status,
        roster_id=1,
        membership_segment="segment-1",
        eligible_slot_indices=eligible_slot_indices,
        eligible_positions=("PG",),
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
            distribution=ProjectionDistribution.from_weighted_observations(((expected, 1),)),
            reasons=(),
        ),
        missing_projection_reason=None,
        completed_fantasy_score=None,
        finalized_at=None,
    )


def _state(
    *,
    slots: tuple[StarterSlot, ...],
    active_slot: int,
    active_indices: tuple[int, ...],
    scheduled_indices: tuple[int, ...],
) -> TeamWeekState:
    """Build a state with one active starter and one upcoming opportunity."""

    active = _opportunity(
        "active",
        "active-game",
        start=NOW - timedelta(hours=1),
        status=PlanningGameStatus.ACTIVE,
        eligible_slot_indices=active_indices,
        expected=20,
    )
    scheduled = _opportunity(
        "scheduled",
        "scheduled-game",
        start=NOW + timedelta(hours=1),
        status=PlanningGameStatus.SCHEDULED,
        eligible_slot_indices=scheduled_indices,
        expected=100,
    )
    return TeamWeekState(
        league_id="league-1",
        season="2026",
        week=1,
        roster_id=1,
        decision_time=NOW,
        starter_slots=slots,
        roster_player_ids=("active", "scheduled"),
        observed_starters=(ObservedStarter(active_slot, "active", ("PG",)),),
        opportunities=(active, scheduled),
        scoring_policy_version="fixture-scoring",
        league_configuration_version="fixture-league",
        manager_policy_version="fixture-policy",
        projection_model_version="fixture-model",
        input_version="fixture-inputs",
    )


def test_plan_preserves_active_starter_while_adding_upcoming_player() -> None:
    """Keep an eligible performance alive when a nonconflicting batch is added."""

    state = _state(
        slots=(StarterSlot(0, "G"), StarterSlot(1, "UTIL")),
        active_slot=0,
        active_indices=(0, 1),
        scheduled_indices=(0, 1),
    )

    plan = build_weekly_plan(state, policy=WeeklyPlanPolicyConfig(scenario_count=5))

    assert {item.player_id for item in plan.desired_assignments} == {"active", "scheduled"}
    assert all(move.target_slot_index is not None for move in plan.moves)


def test_plan_realigns_active_starter_to_fit_upcoming_player() -> None:
    """Move an active starter between starter slots when the batch needs its old slot."""

    state = _state(
        slots=(StarterSlot(0, "G"), StarterSlot(1, "UTIL")),
        active_slot=0,
        active_indices=(0, 1),
        scheduled_indices=(0,),
    )

    plan = build_weekly_plan(state, policy=WeeklyPlanPolicyConfig(scenario_count=5))

    assert tuple(item.player_id for item in plan.desired_assignments) == (
        "scheduled",
        "active",
    )
    assert any(
        move.player_id == "active" and move.source_slot_index == 0 and move.target_slot_index == 1
        for move in plan.moves
    )


def test_active_starter_wins_when_capacity_is_insufficient() -> None:
    """Forbid an upcoming score from implicitly forfeiting an active performance."""

    state = _state(
        slots=(StarterSlot(0, "G"),),
        active_slot=0,
        active_indices=(0,),
        scheduled_indices=(0,),
    )

    decision = score_weekly_options(
        state,
        config=WeeklyPlanPolicyConfig(scenario_count=5),
    )

    assert tuple(item.player_id for item in decision.selected.assignments) == ("active",)


def test_locked_player_is_not_reintroduced_as_an_active_candidate() -> None:
    """Keep a later in-progress game from duplicating an already fixed player."""

    state = _state(
        slots=(StarterSlot(0, "G"), StarterSlot(1, "UTIL")),
        active_slot=0,
        active_indices=(0, 1),
        scheduled_indices=(0, 1),
    )
    completed = _opportunity(
        "active",
        "locked-game",
        start=NOW - timedelta(hours=4),
        status=PlanningGameStatus.FINAL,
        eligible_slot_indices=(0, 1),
        expected=25,
    )
    completed = replace(
        completed,
        completed_fantasy_score=25,
        finalized_at=NOW - timedelta(hours=2),
    )
    state = replace(
        state,
        opportunities=state.opportunities + (completed,),
        fixed_slots=(
            FixedSlot(
                0,
                "G",
                "active",
                "locked-game",
                25,
                NOW - timedelta(hours=1),
                "lock-1",
                "fixture",
            ),
        ),
    )

    plan = build_weekly_plan(state, policy=WeeklyPlanPolicyConfig(scenario_count=5))

    assert tuple(item.player_id for item in plan.desired_assignments) == (
        "active",
        "scheduled",
    )


def test_equivalent_slot_variants_reuse_the_same_terminal_evaluation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Avoid repeating rollouts when only current player placement differs."""

    state = _state(
        slots=(StarterSlot(0, "G"), StarterSlot(1, "UTIL"), StarterSlot(2, "UTIL")),
        active_slot=0,
        active_indices=(0, 1, 2),
        scheduled_indices=(0, 1, 2),
    )
    second_scheduled = _opportunity(
        "second-scheduled",
        "second-scheduled-game",
        start=NOW + timedelta(hours=1),
        status=PlanningGameStatus.SCHEDULED,
        eligible_slot_indices=(0, 1, 2),
        expected=90,
    )
    state = replace(
        state,
        roster_player_ids=state.roster_player_ids + ("second-scheduled",),
        opportunities=state.opportunities + (second_scheduled,),
    )
    original = scoring_module.assignment_terminal_value
    calls = 0

    def counted(*args: object, **kwargs: object) -> float:
        """Count expensive terminal evaluations while preserving their behavior."""

        nonlocal calls
        calls += 1
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(scoring_module, "assignment_terminal_value", counted)

    score_weekly_options(state, config=WeeklyPlanPolicyConfig(scenario_count=5))

    assert calls == 10


def test_replay_tracks_an_active_starter_in_its_new_slot() -> None:
    """Update replay's active-slot index after legal starter realignment."""

    state = _state(
        slots=(StarterSlot(0, "G"), StarterSlot(1, "UTIL")),
        active_slot=0,
        active_indices=(0, 1),
        scheduled_indices=(0,),
    )

    assert active_target_slots(
        desired={0: "scheduled", 1: "active"},
        active={"active": 0},
        planning_state=state,
        event_id="planning:test",
    ) == {"active": 1}


def test_replay_rejects_an_implicitly_benched_active_starter() -> None:
    """Retain a stable failure instead of silently forfeiting eligibility."""

    state = _state(
        slots=(StarterSlot(0, "G"), StarterSlot(1, "UTIL")),
        active_slot=0,
        active_indices=(0, 1),
        scheduled_indices=(0,),
    )

    with pytest.raises(FullAdvisorReplayError, match="active_player_omitted:active:slot=0"):
        active_target_slots(
            desired={0: "scheduled"},
            active={"active": 0},
            planning_state=state,
            event_id="planning:test",
        )


def test_replay_rejects_an_ineligible_active_starter_target() -> None:
    """Fail closed when a target keeps the player but violates position eligibility."""

    state = _state(
        slots=(StarterSlot(0, "G"), StarterSlot(1, "C")),
        active_slot=0,
        active_indices=(0,),
        scheduled_indices=(0,),
    )

    with pytest.raises(FullAdvisorReplayError, match="active_player_ineligible:active:slot=1"):
        active_target_slots(
            desired={0: "scheduled", 1: "active"},
            active={"active": 0},
            planning_state=state,
            event_id="planning:test",
        )
