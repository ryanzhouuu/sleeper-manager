"""A final too close to advise is still notified and settles its watch."""

import asyncio
import json
from dataclasses import replace
from hashlib import sha256

from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.domain.planning import PlanStatus
from sleeper_manager.domain.planning_actions import UndecidedScore
from sleeper_manager.persistence.base import RecommendationStatus
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityKey
from sleeper_manager.workflows.daily_plan import _is_quiet_plan, _should_notify
from sleeper_manager.workflows.postgame_lock_in import LOCK_IN_DECISION_TYPE, run_postgame_lock_in
from tests.sleeper_manager.workflows.postgame_lock_in_support import (
    POLICY,
    POSTGAME,
    SECOND_POLL,
    _box,
    _fetcher,
    _prepare,
    _summary_result,
)
from tests.sleeper_manager.workflows.test_plan_rendering import DEADLINE, _assignment, _plan
from tests.sleeper_manager.workflows.test_plan_rendering import NOW as PLAN_NOW


def test_close_call_becomes_up_to_you_advice_that_settles_its_row(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, inputs, notifications, sender = await _prepare(tmp_path)
        summary = _summary_result(_box("401", points=20), _box("402", points=8))
        for poll, decided_at in enumerate((POSTGAME, SECOND_POLL)):
            await run_postgame_lock_in(
                "g1",
                inputs,
                decision_time=decided_at,
                repository=repository,
                notifications=notifications,
                fetch_summary=_fetcher(summary),
                runtime_policy=POLICY,
                open_sleeper_url="https://sleeper.com",
                poll_id=f"wake-{poll}",
            )

        pending = await repository.list_pending_recommendations(
            "league-1", 1, decision_type=LOCK_IN_DECISION_TYPE
        )
        (choice,) = [row for row in pending if row.player_id == "p2"]
        trace = json.loads(choice.trace_json)
        assert choice.title == "Lock or pass p2?"
        assert choice.status is RecommendationStatus.PENDING
        assert trace["kind"] == "up_to_you"
        assert trace["choice"]["score"] == 8
        assert trace["choice"]["lock_slot_position"] is not None
        row = await repository.get_lock_in_opportunity(
            LockInOpportunityKey("league-1", 1, 1, "p2", "g1")
        )
        assert row is not None
        assert row.status is LockInOpportunityStatus.ACTIONABLE
        assert row.current_recommendation_id == choice.recommendation_id
        assert any("Up to you on p2's 8.00 pts" in sent.message for sent in sender.messages)

    asyncio.run(exercise())


def test_a_plan_with_only_close_calls_is_still_sent() -> None:
    assignments = (_assignment(0, "G", "p1"),)
    plan = _plan(
        status=PlanStatus.NO_ACTION,
        observed_assignments=assignments,
        desired_assignments=assignments,
        moves=(),
        undecided_scores=(UndecidedScore("p1", "g1", 0.5, 0.4, score=10.0, deadline=DEADLINE),),
    )

    assert not _is_quiet_plan(plan)
    assert _should_notify(plan, DEADLINE, PLAN_NOW)
    assert not _should_notify(plan, PLAN_NOW, PLAN_NOW)


def test_close_calls_change_plan_identity_only_by_which_finals_they_are() -> None:
    plan = _plan()
    close = replace(plan, undecided_scores=(UndecidedScore("p1", "g1", 0.5, 0.4),))
    shifted = replace(plan, undecided_scores=(UndecidedScore("p1", "g1", 0.6, 0.3),))

    assert (
        plan.material_hash == sha256(repr(plan._decided_material_components()).encode()).hexdigest()
    )
    assert close.material_hash != plan.material_hash
    assert shifted.material_hash == close.material_hash
