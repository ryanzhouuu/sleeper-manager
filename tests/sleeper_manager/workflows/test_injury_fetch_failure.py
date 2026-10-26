"""Keep actionable advice deliverable while injury evidence is explicitly unavailable."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from sleeper_manager.domain.nba import DataQualityState
from sleeper_manager.domain.planning import PlanningReasonCode, PlanStatus
from sleeper_manager.workflows.planning_inputs import (
    LivePlanningInputs,
    build_live_team_week_state,
)
from sleeper_manager.workflows.postgame_lock_in import run_postgame_lock_in
from tests.sleeper_manager.workflows import postgame_lock_in_support as postgame
from tests.sleeper_manager.workflows.test_daily_plan import (
    NOW,
    _inputs,
    _profile,
    _projections,
    _run_async,
)


def _failed_injuries(inputs: LivePlanningInputs) -> LivePlanningInputs:
    """Represent a failed request without retaining any previous injury designations."""
    return replace(
        inputs,
        availability_results=tuple(
            replace(
                result,
                records=(),
                quality=replace(result.quality, state=DataQualityState.ERROR, record_count=0),
            )
            for result in inputs.availability_results
        ),
    )


def test_injury_failure_still_delivers_an_actionable_lineup_notification(tmp_path: Path) -> None:
    inputs = _failed_injuries(
        _inputs(_profile(starter_ids=("p1", None)), projections=_projections())
    )

    result, sender, _, _ = asyncio.run(_run_async(tmp_path, inputs))

    assert result.outcome == "notified"
    assert result.plan.status is PlanStatus.DEGRADED
    assert result.plan.blocking_reasons == ()
    assert result.plan.expected_terminal_score is not None
    assert len(sender.messages) == 1
    assert "Start Ben at UTIL." in sender.messages[0].message
    assert "injury updates are unavailable" in sender.messages[0].message.lower()
    assert "provisional" in sender.messages[0].message.lower()
    assert result.recommendation is not None
    trace = json.loads(result.recommendation.trace_json)
    assert trace["warnings"] == ["injury_data_unavailable"]
    state = build_live_team_week_state(inputs, decision_time=NOW)
    assert all(opportunity.availability_status == "unknown" for opportunity in state.opportunities)


def test_injury_failure_keeps_no_action_plans_quiet(tmp_path: Path) -> None:
    inputs = _failed_injuries(_inputs(projections=_projections()))

    result, sender, _, _ = asyncio.run(_run_async(tmp_path, inputs))

    assert result.outcome == "no_action"
    assert result.plan.status is PlanStatus.DEGRADED
    assert sender.messages == []


def test_expired_injury_evidence_still_blocks_planning() -> None:
    inputs = _inputs(projections=_projections())
    old = NOW - timedelta(minutes=16)
    result = inputs.availability_results[0]
    inputs = replace(
        inputs,
        freshness_policy=replace(
            inputs.freshness_policy, max_availability_age=timedelta(minutes=15)
        ),
        availability_results=(replace(result, quality=replace(result.quality, retrieved_at=old)),),
    )

    state = build_live_team_week_state(inputs, decision_time=NOW)

    assert PlanningReasonCode.STALE_NBA_STATE in state.blocking_reasons


def test_postgame_lock_advice_survives_injury_fetch_failure(tmp_path: Path) -> None:
    async def exercise() -> None:
        repository, inputs, notifications, sender = await postgame._prepare(tmp_path)
        inputs = _failed_injuries(inputs)
        fetch = postgame._fetcher(
            postgame._summary_result(
                postgame._box("401", points=30), postgame._box("402", points=8)
            )
        )
        for at, poll_id in ((postgame.POSTGAME, "first"), (postgame.SECOND_POLL, "second")):
            notifications._clock = lambda at=at: at
            await run_postgame_lock_in(
                "g1",
                inputs,
                decision_time=at,
                repository=repository,
                notifications=notifications,
                fetch_summary=fetch,
                runtime_policy=postgame.POLICY,
                open_sleeper_url="https://sleeper.com",
                poll_id=poll_id,
                player_names={"p1": "Ann", "p2": "Ben"},
            )

        locks = [message for message in sender.messages if "Lock Ann" in message.message]
        assert len(locks) == 1
        assert "injury updates are unavailable" in locks[0].message.lower()
        assert "provisional" in locks[0].message.lower()

    asyncio.run(exercise())
