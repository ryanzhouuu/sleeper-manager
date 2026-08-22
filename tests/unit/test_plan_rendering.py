from dataclasses import replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from sleeper_manager.domain.planning import (
    LineupMove,
    PlanConfidence,
    PlanDistributionSummary,
    PlannedAssignment,
    PlanningReasonCode,
    PlanStatus,
    WeeklyPlan,
)
from sleeper_manager.workflows.plan_rendering import render_weekly_plan

NOW = datetime(2026, 1, 5, 12, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=50)
CHICAGO = ZoneInfo("America/Chicago")
NAMES = {"p1": "Bob", "p2": "Alice", "a": "Ann", "b": "Ben", "c": "Cal"}


class FixtureReason(StrEnum):
    MYSTERY = "mystery_reason"


def _assignment(index: int, position: str, player_id: str | None) -> PlannedAssignment:
    return PlannedAssignment(index, position, player_id)


def _plan(**overrides: object) -> WeeklyPlan:
    values: dict[str, object] = {
        "league_id": "league-1",
        "season": "2026",
        "week": 1,
        "roster_id": 1,
        "decision_time": NOW,
        "status": PlanStatus.ACTION_REQUIRED,
        "observed_assignments": (
            _assignment(0, "G", "p1"),
            _assignment(1, "UTIL", None),
        ),
        "desired_assignments": (
            _assignment(0, "G", "p1"),
            _assignment(1, "UTIL", "p2"),
        ),
        "moves": (LineupMove("p2", None, 1, DEADLINE),),
        "confidence": PlanConfidence.HIGH,
        "planner_version": "weekly-planner-v1",
        "manager_policy_version": "policy-v1",
        "expected_terminal_score": 104.8,
        "observed_terminal_score": 100.0,
    }
    values.update(overrides)
    return WeeklyPlan(**values)


def test_action_required_messages_lead_with_the_imperative() -> None:
    rendered = render_weekly_plan(_plan(), player_names=NAMES, local_timezone=CHICAGO)

    sections = rendered.message.split("\n\n")
    assert rendered.title == "Lineup move required"
    assert sections[0] == "Start Alice in UTIL."
    assert sections[1] == "Expected terminal weekly value improves by 4.8 points."
    assert sections[-1] == "Complete before Mon 6:50 AM CST."


def test_bench_moves_and_swaps_render_in_order() -> None:
    plan = _plan(
        observed_assignments=(
            _assignment(0, "G", "p1"),
            _assignment(1, "UTIL", "p2"),
        ),
        desired_assignments=(
            _assignment(0, "G", "p2"),
            _assignment(1, "UTIL", "p1"),
        ),
        moves=(
            LineupMove("p1", 0, None, DEADLINE),
            LineupMove("p2", 1, 0, DEADLINE),
            LineupMove("p1", None, 1, DEADLINE),
        ),
    )

    imperative = render_weekly_plan(plan, player_names=NAMES).message.split("\n\n")[0]

    assert imperative == ("Move Bob to the bench. Move Alice from UTIL to G. Start Bob in UTIL.")


def test_three_way_cycles_render_every_step() -> None:
    plan = _plan(
        observed_assignments=(
            _assignment(0, "G", "a"),
            _assignment(1, "UTIL", "b"),
            _assignment(2, "UTIL", "c"),
        ),
        desired_assignments=(
            _assignment(0, "G", "b"),
            _assignment(1, "UTIL", "c"),
            _assignment(2, "UTIL", "a"),
        ),
        moves=(
            LineupMove("a", 0, None, DEADLINE),
            LineupMove("b", 1, 0, DEADLINE),
            LineupMove("c", 2, 1, DEADLINE),
            LineupMove("a", None, 2, DEADLINE),
        ),
    )

    imperative = render_weekly_plan(plan, player_names=NAMES).message.split("\n\n")[0]

    assert imperative == (
        "Move Ann to the bench. Move Ben from UTIL 1 to G. "
        "Move Cal from UTIL 2 to UTIL 1. Start Ann in UTIL 2."
    )


def test_missing_names_fall_back_to_player_ids() -> None:
    rendered = render_weekly_plan(_plan())

    assert rendered.message.split("\n\n")[0] == "Start p2 in UTIL."


def test_unknown_reason_codes_fall_back_to_readable_text() -> None:
    plan = _plan(warnings=(PlanningReasonCode.STALE_NBA_STATE, FixtureReason.MYSTERY))

    rendered = render_weekly_plan(plan)

    assert "Notes: NBA schedule data may be stale; mystery reason." in rendered.message


def test_blocked_plans_avoid_imperatives() -> None:
    plan = _plan(
        status=PlanStatus.BLOCKED,
        blocking_reasons=(PlanningReasonCode.DEADLINE_ELAPSED,),
        moves=tuple(replace(move, deadline=NOW - timedelta(minutes=1)) for move in _plan().moves),
    )

    rendered = render_weekly_plan(plan)

    assert rendered.title == "Lineup planning blocked"
    assert rendered.message.startswith("Advice is blocked: the lineup deadline has passed.")
    assert "Start" not in rendered.message
    assert "Move" not in rendered.message


def test_no_action_plans_do_not_prescribe_moves() -> None:
    assignments = (
        _assignment(0, "G", "p1"),
        _assignment(1, "UTIL", "p2"),
    )
    plan = _plan(
        status=PlanStatus.NO_ACTION,
        observed_assignments=assignments,
        desired_assignments=assignments,
        moves=(),
    )

    rendered = render_weekly_plan(plan)

    assert rendered.title == "Lineup is set"
    assert rendered.message == "Your lineup already matches the weekly plan."


def test_degraded_plans_surface_warnings_and_confidence() -> None:
    plan = _plan(
        status=PlanStatus.DEGRADED,
        warnings=(PlanningReasonCode.STALE_NBA_STATE,),
        confidence=PlanConfidence.LOW,
    )

    rendered = render_weekly_plan(plan)

    assert rendered.title == "Lineup check degraded"
    assert "Notes: NBA schedule data may be stale." in rendered.message
    assert "Confidence: low." in rendered.message


def test_degraded_plans_with_moves_keep_their_instructions() -> None:
    plan = _plan(
        status=PlanStatus.DEGRADED,
        warnings=(PlanningReasonCode.STALE_NBA_STATE,),
        confidence=PlanConfidence.LOW,
    )

    rendered = render_weekly_plan(plan, player_names=NAMES, local_timezone=CHICAGO)

    sections = rendered.message.split("\n\n")
    assert sections[0] == "Start Alice in UTIL."
    assert sections[-1] == "Complete before Mon 6:50 AM CST."
    assert "Notes: NBA schedule data may be stale." in rendered.message


def test_schedule_reasons_explain_why_the_change_matters() -> None:
    plan = _plan(
        schedule_assumptions=(
            f"game g1 assumed to start {(NOW + timedelta(hours=1)).isoformat()}",
            "2 later opportunities remain replannable",
        )
    )

    rendered = render_weekly_plan(plan, local_timezone=CHICAGO)

    schedule_section = rendered.message.split("\n\n")[2]
    assert (
        "This matters because the first affected game tips off Mon 7:00 AM CST." in schedule_section
    )
    assert "2 later opportunities remain replannable." in schedule_section


def test_value_clauses_are_omitted_without_scores() -> None:
    rendered = render_weekly_plan(_plan(expected_terminal_score=None, observed_terminal_score=None))

    assert "points" not in rendered.message


def test_approximation_estimates_are_disclosed() -> None:
    plan = _plan(
        distribution_summary=PlanDistributionSummary(
            scenario_count=2000,
            seed=0,
            approximation="complete_assignment_rollout",
            perfect_information_bound=110.0,
        )
    )

    rendered = render_weekly_plan(plan)

    assert (
        "Values are modeled estimates over 2000 scenarios "
        "(complete_assignment_rollout), not guarantees." in rendered.message
    )


def test_deadlines_use_the_earliest_affected_move() -> None:
    plan = _plan(
        observed_assignments=(
            _assignment(0, "G", "p1"),
            _assignment(1, "UTIL", "p2"),
        ),
        desired_assignments=(
            _assignment(0, "G", "p2"),
            _assignment(1, "UTIL", "p1"),
        ),
        moves=(
            LineupMove("p1", 0, None, DEADLINE + timedelta(hours=3)),
            LineupMove("p2", 1, 0, DEADLINE),
            LineupMove("p1", None, 1, DEADLINE + timedelta(hours=3)),
        ),
    )

    rendered = render_weekly_plan(plan, local_timezone=CHICAGO)

    assert rendered.message.endswith("Complete before Mon 6:50 AM CST.")
