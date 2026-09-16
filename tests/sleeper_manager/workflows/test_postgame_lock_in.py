"""Postgame wait, notify, and supersede coverage for live Lock-In."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from sleeper_manager.domain.lock_in import LockInEvaluationKind, LockInOpportunityStatus
from sleeper_manager.domain.nba import (
    DataQualityState,
    GameStatus,
)
from sleeper_manager.persistence.base import RecommendationStatus
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityKey
from sleeper_manager.workflows.postgame_lock_in import (
    LOCK_IN_DECISION_TYPE,
    run_postgame_lock_in,
)
from tests.sleeper_manager.workflows.postgame_lock_in_support import (
    POLICY,
    POSTGAME,
    SECOND_POLL,
    _box,
    _fetcher,
    _prepare,
    _run_poll,
    _summary_result,
)


def test_partial_or_in_progress_summary_waits_without_notification(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Incomplete ESPN evidence cannot count as a stable independent poll."""

    async def exercise() -> None:
        boxes = (_box("401", points=20), _box("402", points=8))
        for index, result in enumerate(
            (
                _summary_result(*boxes, status=GameStatus.IN_PROGRESS),
                _summary_result(*boxes, status=GameStatus.POSTPONED),
                _summary_result(*boxes, status=GameStatus.CANCELED),
                _summary_result(*boxes, quality=DataQualityState.PARTIAL),
                _summary_result(*boxes, quality=DataQualityState.STALE),
                _summary_result(_box("402", points=8)),
            )
        ):
            case_dir = tmp_path / str(index)
            case_dir.mkdir()
            repository, sender, outcome = await _run_poll(case_dir, result)
            stored = await repository.get_lock_in_opportunity(
                LockInOpportunityKey("league-1", 1, 1, "p1", "g1")
            )
            assert outcome.outcome == "wait"
            assert stored is not None
            assert stored.consecutive_direct_poll_count == 0
            assert sender.messages == []

    asyncio.run(exercise())


def test_two_identical_finals_notify_lock_with_acknowledgement_actions(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """Require two distinct scheduled wakes before emitting Lock advice."""

    async def exercise() -> None:
        repository, inputs, notifications, sender = await _prepare(tmp_path)
        fetch = _fetcher(
            _summary_result(_box("401", points=20), _box("402", points=8, did_play=False))
        )
        first = await run_postgame_lock_in(
            "g1",
            inputs,
            decision_time=POSTGAME,
            repository=repository,
            notifications=notifications,
            fetch_summary=fetch,
            runtime_policy=POLICY,
            open_sleeper_url="https://sleeper.com",
            poll_id="wake-1",
            player_names={"p1": "Ann", "p2": "Ben"},
        )
        notifications._clock = lambda: SECOND_POLL
        second = await run_postgame_lock_in(
            "g1",
            inputs,
            decision_time=SECOND_POLL,
            repository=repository,
            notifications=notifications,
            fetch_summary=fetch,
            runtime_policy=POLICY,
            open_sleeper_url="https://sleeper.com",
            poll_id="wake-2",
            player_names={"p1": "Ann", "p2": "Ben"},
        )
        stored = await repository.get_lock_in_opportunity(
            LockInOpportunityKey("league-1", 1, 1, "p1", "g1")
        )
        assert first.outcome == "wait"
        assert second.outcome == "notified"
        assert second.evaluation is not None
        assert second.evaluation.kind is LockInEvaluationKind.LOCK
        assert stored is not None
        assert stored.status is LockInOpportunityStatus.ACTIONABLE
        assert stored.consecutive_direct_poll_count == 2
        assert stored.score_revision == 1
        assert len(sender.messages) == 1
        assert [action.label for action in sender.messages[0].actions] == [
            "Locked",
            "Passed",
            "Open Sleeper",
        ]
        assert second.recommendation is not None
        assert second.recommendation.decision_type == LOCK_IN_DECISION_TYPE
        notifications._clock = lambda: SECOND_POLL + timedelta(minutes=5)
        retry = await run_postgame_lock_in(
            "g1",
            inputs,
            decision_time=SECOND_POLL + timedelta(minutes=5),
            repository=repository,
            notifications=notifications,
            fetch_summary=fetch,
            runtime_policy=POLICY,
            open_sleeper_url="https://sleeper.com",
            poll_id="wake-3",
            player_names={"p1": "Ann", "p2": "Ben"},
        )
        assert retry.outcome == "duplicate"
        assert len(sender.messages) == 1

    asyncio.run(exercise())


def test_changed_fingerprint_resets_and_supersedes_unacknowledged_advice(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """A corrected box score returns to finalizing and replaces pending advice."""

    async def exercise() -> None:
        repository, inputs, notifications, sender = await _prepare(tmp_path)
        first_summary = _summary_result(_box("401", points=20), _box("402", points=8))
        corrected = _summary_result(_box("401", points=24), _box("402", points=8))
        await run_postgame_lock_in(
            "g1",
            inputs,
            decision_time=POSTGAME,
            repository=repository,
            notifications=notifications,
            fetch_summary=_fetcher(first_summary),
            runtime_policy=POLICY,
            open_sleeper_url="https://sleeper.com",
            poll_id="wake-1",
            player_names={"p1": "Ann"},
        )
        notifications._clock = lambda: SECOND_POLL
        await run_postgame_lock_in(
            "g1",
            inputs,
            decision_time=SECOND_POLL,
            repository=repository,
            notifications=notifications,
            fetch_summary=_fetcher(first_summary),
            runtime_policy=POLICY,
            open_sleeper_url="https://sleeper.com",
            poll_id="wake-2",
            player_names={"p1": "Ann"},
        )
        first_rec = (
            await repository.list_pending_recommendations(
                "league-1", 1, decision_type=LOCK_IN_DECISION_TYPE
            )
        )[0]
        third_at = SECOND_POLL + timedelta(minutes=5)
        notifications._clock = lambda: third_at
        changed = await run_postgame_lock_in(
            "g1",
            inputs,
            decision_time=third_at,
            repository=repository,
            notifications=notifications,
            fetch_summary=_fetcher(corrected),
            runtime_policy=POLICY,
            open_sleeper_url="https://sleeper.com",
            poll_id="wake-3",
            player_names={"p1": "Ann"},
        )
        stored = await repository.get_lock_in_opportunity(
            LockInOpportunityKey("league-1", 1, 1, "p1", "g1")
        )
        superseded = await repository.get_recommendation(first_rec.recommendation_id)
        assert changed.outcome == "wait"
        assert stored is not None
        assert stored.consecutive_direct_poll_count == 1
        assert stored.score_revision == 1
        assert stored.status is LockInOpportunityStatus.FINALIZING
        assert superseded is not None
        assert superseded.status is RecommendationStatus.SUPERSEDED

    asyncio.run(exercise())
