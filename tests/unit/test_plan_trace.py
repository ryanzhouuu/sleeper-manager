import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sleeper_manager.domain.planning import (
    FixedSlot,
    FreshnessSummary,
    LineupMove,
    PassedOpportunity,
    PlanConfidence,
    PlannedAssignment,
    PlanStatus,
    SourceLineage,
    WeeklyPlan,
)
from sleeper_manager.workflows.notification_loop import recommendation_id_for
from sleeper_manager.workflows.plan_rendering import serialize_weekly_plan_trace

NOW = datetime(2026, 1, 5, 12, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=50)


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


def test_serialize_weekly_plan_trace_is_stable_json() -> None:
    plan = _plan()
    first = json.loads(serialize_weekly_plan_trace(plan, trigger="daily"))
    second = json.loads(serialize_weekly_plan_trace(plan, trigger="daily"))

    assert first == second
    assert first["schema_version"] == 1
    assert first["kind"] == "weekly_lineup"
    assert first["trigger"] == "daily"
    assert first["plan_id"] == plan.plan_id
    assert first["material_hash"] == plan.material_hash
    assert first["status"] == "action_required"
    assert first["moves"] == [
        {
            "player_id": "p2",
            "source_slot_index": None,
            "target_slot_index": 1,
            "deadline": DEADLINE.isoformat(),
        }
    ]


def test_recommendation_id_matches_notification_loop() -> None:
    key = "league-1:1:weekly_lineup:abc123"
    assert recommendation_id_for(key) == recommendation_id_for(key)
    assert len(recommendation_id_for(key)) == 32


def test_trace_includes_trigger() -> None:
    daily = json.loads(serialize_weekly_plan_trace(_plan(), trigger="daily"))
    pre_tipoff = json.loads(serialize_weekly_plan_trace(_plan(), trigger="pre_tipoff"))
    assert daily["trigger"] == "daily"
    assert pre_tipoff["trigger"] == "pre_tipoff"


def test_trace_reflects_explanation_drift_without_changing_material_hash() -> None:
    plan = _plan()
    drifted = replace(plan, expected_terminal_score=999.5)
    assert plan.material_hash == drifted.material_hash
    assert json.loads(serialize_weekly_plan_trace(plan))["expected_terminal_score"] == 104.8
    assert json.loads(serialize_weekly_plan_trace(drifted))["expected_terminal_score"] == 999.5


def test_trace_includes_acknowledgement_constraints_and_freshness() -> None:
    plan = _plan(
        fixed_slots=(
            FixedSlot(
                slot_index=0,
                slot_position="G",
                player_id="p1",
                game_id="g0",
                accepted_fantasy_score=12.5,
                decision_time=NOW - timedelta(hours=1),
                decision_id="lock-1",
                provenance="repository_acknowledgement_v1",
            ),
        ),
        passed_opportunities=(
            PassedOpportunity(
                player_id="p3",
                game_id="g9",
                decision_time=NOW - timedelta(minutes=30),
                decision_id="pass-1",
                provenance="repository_acknowledgement_v1",
            ),
        ),
        freshness=FreshnessSummary(
            (
                SourceLineage(
                    "sleeper",
                    "league-v1",
                    NOW - timedelta(hours=2),
                    NOW - timedelta(minutes=5),
                ),
                SourceLineage("espn", "schedule-v1", NOW - timedelta(hours=1), None),
            )
        ),
    )
    payload = json.loads(serialize_weekly_plan_trace(plan, trigger="daily"))

    assert payload["fixed_slots"] == [
        {
            "slot_index": 0,
            "slot_position": "G",
            "player_id": "p1",
            "game_id": "g0",
            "accepted_fantasy_score": 12.5,
            "decision_time": (NOW - timedelta(hours=1)).isoformat(),
            "decision_id": "lock-1",
            "provenance": "repository_acknowledgement_v1",
        }
    ]
    assert payload["passed_opportunities"] == [
        {
            "player_id": "p3",
            "game_id": "g9",
            "decision_time": (NOW - timedelta(minutes=30)).isoformat(),
            "decision_id": "pass-1",
            "provenance": "repository_acknowledgement_v1",
        }
    ]
    assert payload["freshness"]["sources"] == [
        {
            "source": "sleeper",
            "version": "league-v1",
            "available_as_of": (NOW - timedelta(hours=2)).isoformat(),
            "retrieved_at": (NOW - timedelta(minutes=5)).isoformat(),
        },
        {
            "source": "espn",
            "version": "schedule-v1",
            "available_as_of": (NOW - timedelta(hours=1)).isoformat(),
            "retrieved_at": None,
        },
    ]
