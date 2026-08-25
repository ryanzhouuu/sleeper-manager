import asyncio
import json
from datetime import datetime, timedelta

from test_daily_plan import (
    NOW,
    _inputs,
    _profile,
    _projections,
    _workflow,
)

from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.persistence.base import RecommendationStatus
from sleeper_manager.workflows.pre_tipoff_check import run_pre_tipoff_check

GAME_START = NOW + timedelta(hours=2)
DEADLINE = GAME_START - timedelta(minutes=10)


async def _run_pre_tipoff(
    tmp_path,
    inputs,
    *,
    decision_time: datetime = NOW,
    clock=lambda: NOW,
    repository=None,
    notifications=None,
    sender=None,
):
    if repository is None or notifications is None or sender is None:
        repository, sender, notifications = _workflow(tmp_path, clock=clock)
        await repository.initialize()
    return await run_pre_tipoff_check(
        inputs,
        decision_time=decision_time,
        repository=repository,
        notifications=notifications,
        open_sleeper_url="https://sleeper.com/league",
        player_names={"p1": "Ann", "p2": "Ben"},
        policy=WeeklyPlanPolicyConfig(scenario_count=8),
        clock=clock,
    )


def test_pre_tipoff_records_trigger_in_trace(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        inputs = _inputs(
            _profile(starter_ids=("p1", None)),
            projections=_projections(p1_expected=10.0, p2_expected=30.0),
        )
        repository, sender, notifications = _workflow(tmp_path)
        await repository.initialize()
        result = await _run_pre_tipoff(
            tmp_path,
            inputs,
            repository=repository,
            notifications=notifications,
            sender=sender,
        )

        assert result.outcome == "notified"
        assert result.recommendation is not None
        trace = json.loads(result.recommendation.trace_json)
        assert trace["trigger"] == "pre_tipoff"
        assert len(sender.messages) == 1

    asyncio.run(exercise())


def test_deadline_passed_plan_is_withdrawn(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        inputs = _inputs(
            _profile(starter_ids=("p1", None)),
            projections=_projections(p1_expected=10.0, p2_expected=30.0),
        )
        repository, sender, notifications = _workflow(tmp_path)
        await repository.initialize()
        first = await _run_pre_tipoff(
            tmp_path,
            inputs,
            repository=repository,
            notifications=notifications,
            sender=sender,
        )
        assert first.outcome == "notified"

        late_moment = DEADLINE + timedelta(minutes=1)

        def late_clock() -> datetime:
            return late_moment

        withdrawn = await _run_pre_tipoff(
            tmp_path,
            inputs,
            decision_time=late_moment,
            clock=late_clock,
            repository=repository,
            notifications=notifications,
            sender=sender,
        )

        assert withdrawn.outcome == "withdrawn"
        assert len(sender.messages) == 1
        assert withdrawn.plan.status.value == "blocked"
        assert withdrawn.recommendation is not None
        stored = await repository.get_recommendation(withdrawn.recommendation.recommendation_id)
        assert stored is not None
        assert stored.status is RecommendationStatus.PENDING

    asyncio.run(exercise())
