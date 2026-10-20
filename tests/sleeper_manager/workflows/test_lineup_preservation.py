"""Verify preserved targets, quiet notifications, and retirement of obsolete advice."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig, build_weekly_plan
from sleeper_manager.domain.planning import LineupMove, PlannedAssignment, PlanStatus
from sleeper_manager.persistence.base import RecommendationStatus
from sleeper_manager.workflows.daily_plan import run_daily_plan
from sleeper_manager.workflows.notification_loop import RecommendationRequest
from sleeper_manager.workflows.plan_rendering import (
    WEEKLY_LINEUP_DECISION_TYPE,
    render_weekly_plan,
    serialize_weekly_plan_trace,
)
from sleeper_manager.workflows.planning_inputs import (
    LivePlanningInputs,
    LiveProjectionResult,
    ScheduleResourceResult,
    build_live_team_week_state,
)
from sleeper_manager.workflows.pre_tipoff_check import run_pre_tipoff_check
from tests.sleeper_manager.workflows.test_daily_plan import (
    NOW,
    _game,
    _inputs,
    _quality,
    _snapshot,
    _workflow,
)

POLICY = WeeklyPlanPolicyConfig(scenario_count=8, postgame_scenario_count=8)


def _staggered_inputs() -> LivePlanningInputs:
    """Give the two observed starters different teams and future tipoff times."""
    inputs = _inputs(projections=())
    later = replace(
        _game("g2"),
        start_time=_game().start_time + timedelta(hours=1),
        home_team_id="20",
        away_team_id="21",
    )
    return replace(
        inputs,
        identities=(inputs.identities[0], replace(inputs.identities[1], provider_team_id="20")),
        schedule_results=(
            inputs.schedule_results[0],
            ScheduleResourceResult(
                "espn:team-schedule:20", (later,), _quality(resource="espn:team-schedule:20")
            ),
        ),
        projections=(
            LiveProjectionResult("p1", "g1", _snapshot("p1", "g1", expected=30), None),
            LiveProjectionResult("p2", "g2", _snapshot("p2", "g2", expected=20), None),
        ),
    )


@pytest.mark.parametrize("trigger", ("daily", "pre_tipoff"))
def test_preserved_lineup_is_persisted_without_a_bench_notification(
    tmp_path: Path,
    trigger: str,
) -> None:
    async def exercise() -> None:
        inputs = _staggered_inputs()
        repository, sender, notifications = _workflow(tmp_path)
        await repository.initialize()
        workflow = run_daily_plan if trigger == "daily" else run_pre_tipoff_check

        result = await workflow(
            inputs,
            decision_time=NOW,
            repository=repository,
            notifications=notifications,
            open_sleeper_url="https://example.test/league",
            policy=POLICY,
            clock=lambda: NOW,
        )

        assert result.outcome == "no_action"
        assert sender.messages == []
        assert result.recommendation is not None
        trace = json.loads(result.recommendation.trace_json)
        assert trace["trigger"] == trigger
        assert trace["moves"] == []
        assert trace["desired_assignments"] == trace["observed_assignments"]
        assert {item["player_id"] for item in trace["desired_assignments"]} == {"p1", "p2"}
        assert trace["planner_version"] == "weekly-planner-v4"
        assert "bench" not in result.recommendation.message.casefold()

    asyncio.run(exercise())


def test_corrected_quiet_plan_supersedes_the_old_mass_bench_advice(tmp_path: Path) -> None:
    async def exercise() -> None:
        inputs = _staggered_inputs()
        state = build_live_team_week_state(inputs, decision_time=NOW)
        corrected = build_weekly_plan(state, policy=POLICY)
        assert corrected.status is PlanStatus.NO_ACTION
        assert (
            corrected.material_hash
            != replace(corrected, planner_version="weekly-planner-v3").material_hash
        )
        obsolete = replace(
            corrected,
            planner_version="weekly-planner-v3",
            status=PlanStatus.ACTION_REQUIRED,
            desired_assignments=(
                corrected.desired_assignments[0],
                PlannedAssignment(1, "UTIL", None),
            ),
            moves=(LineupMove("p2", 1, None, _game().start_time - timedelta(minutes=10)),),
        )
        rendered = render_weekly_plan(obsolete)
        repository, sender, notifications = _workflow(tmp_path)
        await repository.initialize()
        old = await notifications.run(
            RecommendationRequest(
                league_id=obsolete.league_id,
                fantasy_week=obsolete.week,
                player_id="p2",
                game_id="g2",
                decision_type=WEEKLY_LINEUP_DECISION_TYPE,
                title=rendered.title,
                message=rendered.message,
                deadline=obsolete.moves[0].deadline,
                policy_version=obsolete.manager_policy_version,
                open_sleeper_url="https://example.test/league",
                trace_json=serialize_weekly_plan_trace(obsolete, trigger="daily"),
                idempotency_key=f"{obsolete.league_id}:{obsolete.week}:weekly_lineup:{obsolete.material_hash}",
            )
        )

        results = [
            await run_daily_plan(
                inputs,
                decision_time=NOW,
                repository=repository,
                notifications=notifications,
                open_sleeper_url="https://example.test/league",
                policy=POLICY,
                clock=lambda: NOW,
            )
            for _ in range(2)
        ]

        assert all(result.outcome == "no_action" for result in results)
        assert len(sender.messages) == 1
        assert results[0].recommendation is not None
        assert results[0].recommendation == results[1].recommendation
        stored_old = await repository.get_recommendation(old.recommendation.recommendation_id)
        assert stored_old is not None and stored_old.status is RecommendationStatus.SUPERSEDED
        pending = await repository.list_pending_recommendations(
            corrected.league_id,
            corrected.week,
            decision_type=WEEKLY_LINEUP_DECISION_TYPE,
        )
        assert [record.recommendation_id for record in pending] == [
            results[0].recommendation.recommendation_id
        ]

    asyncio.run(exercise())
