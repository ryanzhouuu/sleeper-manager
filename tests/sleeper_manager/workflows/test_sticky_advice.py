"""Published advice survives replans that only move within the confidence band."""

import asyncio
import json
from dataclasses import replace
from typing import Any

from sleeper_manager.decisions.team_recommendation import build_team_recommendation
from sleeper_manager.domain.lock_in import LockInDecisionKind
from sleeper_manager.domain.planning import WeeklyPlan
from sleeper_manager.domain.planning_actions import PlannedScoreAction, UndecidedScore
from sleeper_manager.notifications.dispatcher import NotificationDispatcher
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import RecommendationStatus
from sleeper_manager.workflows import daily_plan
from sleeper_manager.workflows.daily_plan import WeeklyLineupWorkflowResult
from sleeper_manager.workflows.notification_loop import NotificationLoop
from sleeper_manager.workflows.plan_rendering import serialize_weekly_plan_trace
from sleeper_manager.workflows.sticky_advice import retains_published_advice
from tests.sleeper_manager.decisions.test_stable_advice import LIVE, _state_near_cutoff
from tests.sleeper_manager.decisions.weekly_plan_support import NOW
from tests.sleeper_manager.workflows.test_daily_plan import _inputs
from tests.sleeper_manager.workflows.test_notification_loop import RecordingSender

NOTES = ("Questionable: Nic Claxton",)


def _base() -> WeeklyPlan:
    plan = build_team_recommendation(_state_near_cutoff(0.55), policy=LIVE)
    game_id = plan.undecided_scores[0].game_id
    return replace(plan, undecided_scores=(UndecidedScore("close", game_id, 0.6, 0.3),))


def _action(plan: WeeklyPlan, kind: LockInDecisionKind, confidence: float) -> PlannedScoreAction:
    undecided = plan.undecided_scores[0]
    locked = kind is LockInDecisionKind.LOCK
    return PlannedScoreAction(
        undecided.player_id,
        undecided.game_id,
        kind,
        50,
        plan.decision_time.replace(year=2099),
        1 if locked else None,
        "C" if locked else None,
        confidence=confidence,
    )


def _with_pass(plan: WeeklyPlan, confidence: float) -> WeeklyPlan:
    return replace(
        plan,
        score_actions=(*plan.score_actions, _action(plan, LockInDecisionKind.PASS, confidence)),
        undecided_scores=(),
    )


def _published(plan: WeeklyPlan) -> dict[str, Any]:
    trace = json.loads(serialize_weekly_plan_trace(plan))
    trace["availability_notes"] = list(NOTES)
    return trace


def _retains(new: WeeklyPlan, published: dict[str, Any], notes: tuple[str, ...] = NOTES) -> bool:
    return retains_published_advice(
        new,
        availability_notes=notes,
        published=published,
        minimum_confidence=0.7,
    )


def _undecided(plan: WeeklyPlan, pass_confidence: float, lock_confidence: float) -> WeeklyPlan:
    game_id = plan.undecided_scores[0].game_id
    return replace(
        plan,
        undecided_scores=(UndecidedScore("close", game_id, pass_confidence, lock_confidence),),
    )


def test_published_pass_survives_a_dip_inside_the_band() -> None:
    base = _base()
    assert _retains(_undecided(base, 0.68, 0.3), _published(_with_pass(base, 0.71)))


def test_published_pass_is_withdrawn_when_confidence_falls_below_the_band() -> None:
    base = _base()
    assert not _retains(_undecided(base, 0.64, 0.3), _published(_with_pass(base, 0.71)))


def test_new_pass_needs_to_clear_the_upper_band_to_replace_silence() -> None:
    base = _base()
    assert _retains(_with_pass(base, 0.72), _published(base))
    assert not _retains(_with_pass(base, 0.75), _published(base))


def test_published_lock_is_withdrawn_only_when_its_own_confidence_collapses() -> None:
    base = _base()
    locked = replace(
        base,
        score_actions=(*base.score_actions, _action(base, LockInDecisionKind.LOCK, 0.72)),
        undecided_scores=(),
    )
    published = _published(locked)
    assert _retains(_undecided(base, 0.3, 0.66), published)
    assert not _retains(_undecided(base, 0.3, 0.6), published)


def test_unchanged_advice_is_not_treated_as_retained() -> None:
    base = _base()
    assert not _retains(base, _published(base))


def test_new_availability_news_replaces_advice_inside_the_band() -> None:
    base = _base()
    published = _published(_with_pass(base, 0.71))
    assert not _retains(_undecided(base, 0.68, 0.3), published, ("Out: Nikola Jokic",))


def test_a_newly_finished_game_replaces_advice() -> None:
    base = _base()
    published = _published(_with_pass(base, 0.71))
    published["score_actions"] = []
    published["undecided_scores"] = []
    assert not _retains(_undecided(base, 0.68, 0.3), published)


def test_manager_confirmation_replaces_advice() -> None:
    base = _base()
    published = _published(_with_pass(base, 0.71))
    published["passed_opportunities"] = [{"player_id": "other", "game_id": "g"}]
    assert not _retains(_undecided(base, 0.68, 0.3), published)


def test_traces_without_undecided_evidence_are_replaced() -> None:
    base = _base()
    published = _published(base)
    del published["undecided_scores"]
    assert not _retains(_with_pass(base, 0.72), published)


class _Harness:
    """Run the real publication path while scripting which plan each replan produces."""

    def __init__(self, tmp_path, monkeypatch, *, fail: bool = False) -> None:  # type: ignore[no-untyped-def]
        self.repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        self.sender = RecordingSender(fail=fail)
        self.state = _state_near_cutoff(0.55)
        self.plan: WeeklyPlan | None = None
        self.loop = NotificationLoop(
            self.repository,
            NotificationDispatcher(self.sender),
            clock=lambda: NOW,
        )

        async def scripted(*args: object, **kwargs: object) -> tuple[WeeklyPlan, object, None]:
            assert self.plan is not None
            return self.plan, self.state, None

        monkeypatch.setattr(daily_plan, "build_current_team_plan", scripted)

    async def replan(
        self, plan: WeeklyPlan, *, daily_confirmation: bool = False
    ) -> WeeklyLineupWorkflowResult:
        self.plan = plan
        return await daily_plan.run_daily_plan(
            _inputs(projections=()),
            decision_time=NOW,
            repository=self.repository,
            notifications=self.loop,
            open_sleeper_url="https://sleeper.com/league",
            policy=LIVE,
            clock=lambda: NOW,
            daily_confirmation=daily_confirmation,
        )


def test_noise_inside_the_band_neither_notifies_nor_cancels_published_advice(
    tmp_path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    async def exercise() -> None:
        harness = _Harness(tmp_path, monkeypatch)
        await harness.repository.initialize()
        base = _base()
        first = await harness.replan(_with_pass(base, 0.71))
        assert first.outcome == "notified"
        assert first.recommendation is not None

        second = await harness.replan(_undecided(base, 0.68, 0.3))

        assert second.outcome == "duplicate"
        assert second.recommendation == first.recommendation
        assert len(harness.sender.messages) == 1
        stored = await harness.repository.get_recommendation(first.recommendation.recommendation_id)
        assert stored is not None
        assert stored.status is RecommendationStatus.PENDING

    asyncio.run(exercise())


def test_a_decisive_drop_still_replaces_published_advice(
    tmp_path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    async def exercise() -> None:
        harness = _Harness(tmp_path, monkeypatch)
        await harness.repository.initialize()
        base = _base()
        first = await harness.replan(_with_pass(base, 0.71))
        assert first.recommendation is not None

        await harness.replan(_undecided(base, 0.6, 0.3))

        stored = await harness.repository.get_recommendation(first.recommendation.recommendation_id)
        assert stored is not None
        assert stored.status is RecommendationStatus.SUPERSEDED

    asyncio.run(exercise())


def test_undelivered_advice_is_not_kept_in_place_of_a_retry(
    tmp_path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    async def exercise() -> None:
        harness = _Harness(tmp_path, monkeypatch, fail=True)
        await harness.repository.initialize()
        base = _base()
        first = await harness.replan(_with_pass(base, 0.71))
        assert first.outcome == "delivery_failed"

        second = await harness.replan(_undecided(base, 0.68, 0.3))

        assert second.outcome != "duplicate"

    asyncio.run(exercise())


def test_scheduled_daily_confirmation_ignores_stickiness(
    tmp_path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    async def exercise() -> None:
        harness = _Harness(tmp_path, monkeypatch)
        await harness.repository.initialize()
        base = _base()
        first = await harness.replan(_with_pass(base, 0.71))
        assert first.recommendation is not None

        await harness.replan(_undecided(base, 0.68, 0.3), daily_confirmation=True)

        stored = await harness.repository.get_recommendation(first.recommendation.recommendation_id)
        assert stored is not None
        assert stored.status is RecommendationStatus.SUPERSEDED

    asyncio.run(exercise())
