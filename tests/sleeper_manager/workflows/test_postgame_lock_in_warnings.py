"""Unavailable warnings, delivery failure, scoring, and automatic last games."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

from sleeper_manager.domain.lock_in import LockInEvaluationKind, LockInOpportunityStatus
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.notifications.dispatcher import NotificationDispatcher
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import RecommendationStatus
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityKey
from sleeper_manager.workflows.lock_in_planning import sync_live_lock_in_opportunities
from sleeper_manager.workflows.notification_loop import NotificationLoop
from sleeper_manager.workflows.planning_inputs import LiveProjectionResult
from sleeper_manager.workflows.postgame_lock_in import (
    LOCK_IN_ACKNOWLEDGEMENT_KINDS,
    LOCK_IN_DECISION_TYPE,
    LOCK_IN_WARNING_TYPE,
    run_postgame_lock_in,
)
from tests.sleeper_manager.workflows.planning_inputs_support import (
    NOW,
    _freshness_policy,
    _game,
    _inputs,
    _schedule_result,
)
from tests.sleeper_manager.workflows.postgame_lock_in_support import (
    GAME_START,
    POLICY,
    POSTGAME,
    SECOND_POLL,
    _box,
    _expected_snapshot,
    _fetcher,
    _live_inputs,
    _prepare,
    _summary_result,
)
from tests.sleeper_manager.workflows.test_notification_loop import RecordingSender


def test_material_policy_change_supersedes_previous_actionable_advice(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Keep only the recommendation produced by the current material evaluation."""

    async def exercise() -> None:
        repository, inputs, notifications, sender = await _prepare(tmp_path)
        summary = _summary_result(_box("401", points=20), _box("402", points=8))
        await run_postgame_lock_in(
            "g1",
            inputs,
            decision_time=POSTGAME,
            repository=repository,
            notifications=notifications,
            fetch_summary=_fetcher(summary),
            runtime_policy=POLICY,
            open_sleeper_url="https://sleeper.com",
            poll_id="wake-1",
        )
        notifications._clock = lambda: SECOND_POLL
        first = await run_postgame_lock_in(
            "g1",
            inputs,
            decision_time=SECOND_POLL,
            repository=repository,
            notifications=notifications,
            fetch_summary=_fetcher(summary),
            runtime_policy=POLICY,
            open_sleeper_url="https://sleeper.com",
            poll_id="wake-2",
        )
        assert first.recommendation is not None
        changed_policy = replace(
            POLICY,
            manager_intent=replace(POLICY.manager_intent, version="intent-v2"),
        )
        third_at = SECOND_POLL + timedelta(minutes=5)
        notifications._clock = lambda: third_at

        second = await run_postgame_lock_in(
            "g1",
            inputs,
            decision_time=third_at,
            repository=repository,
            notifications=notifications,
            fetch_summary=_fetcher(summary),
            runtime_policy=changed_policy,
            open_sleeper_url="https://sleeper.com",
            poll_id="wake-3",
        )

        old = await repository.get_recommendation(first.recommendation.recommendation_id)
        pending = await repository.list_pending_recommendations(
            "league-1", 1, decision_type=LOCK_IN_DECISION_TYPE
        )
        assert second.recommendation is not None
        assert second.recommendation.recommendation_id != first.recommendation.recommendation_id
        assert old is not None and old.status is RecommendationStatus.SUPERSEDED
        assert pending == (second.recommendation,)
        assert len(sender.messages) == 2

    asyncio.run(exercise())


def test_unavailable_warning_omits_acknowledgement_buttons(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Unsafe evidence sends one warning without Locked or Passed actions."""

    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        inputs = replace(
            _live_inputs(),
            projections=(),
        )
        await sync_live_lock_in_opportunities(inputs, repository=repository, observed_at=NOW)
        sender = RecordingSender()
        notifications = NotificationLoop(
            repository,
            NotificationDispatcher(sender),
            acknowledgement_base_url="https://example.test/ack",
            clock=lambda: POSTGAME,
            acknowledgement_kinds=LOCK_IN_ACKNOWLEDGEMENT_KINDS,
        )
        summary = _summary_result(_box("401", points=20), _box("402", points=8))
        await run_postgame_lock_in(
            "g1",
            inputs,
            decision_time=POSTGAME,
            repository=repository,
            notifications=notifications,
            fetch_summary=_fetcher(summary),
            runtime_policy=POLICY,
            open_sleeper_url="https://sleeper.com",
            poll_id="wake-1",
        )
        notifications._clock = lambda: SECOND_POLL
        result = await run_postgame_lock_in(
            "g1",
            inputs,
            decision_time=SECOND_POLL,
            repository=repository,
            notifications=notifications,
            fetch_summary=_fetcher(summary),
            runtime_policy=POLICY,
            open_sleeper_url="https://sleeper.com",
            poll_id="wake-2",
        )
        assert result.outcome == "unavailable"
        assert result.evaluation is not None
        assert result.evaluation.kind is LockInEvaluationKind.UNAVAILABLE
        assert result.recommendation is not None
        assert result.recommendation.decision_type == LOCK_IN_WARNING_TYPE
        assert [action.label for action in sender.messages[0].actions] == ["Open Sleeper"]

    asyncio.run(exercise())


def test_missing_current_mapping_emits_unavailable_warning(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Convert a disappeared live mapping into persisted non-actionable advice."""

    async def exercise() -> None:
        repository, inputs, notifications, sender = await _prepare(tmp_path)
        summary = _summary_result(_box("401", points=20), _box("402", points=8))
        await run_postgame_lock_in(
            "g1",
            inputs,
            decision_time=POSTGAME,
            repository=repository,
            notifications=notifications,
            fetch_summary=_fetcher(summary),
            runtime_policy=POLICY,
            open_sleeper_url="https://sleeper.com",
            poll_id="wake-1",
        )
        unmapped = replace(
            inputs,
            identities=tuple(
                replace(identity, provider_team_id=None) for identity in inputs.identities
            ),
        )
        notifications._clock = lambda: SECOND_POLL

        result = await run_postgame_lock_in(
            "g1",
            unmapped,
            decision_time=SECOND_POLL,
            repository=repository,
            notifications=notifications,
            fetch_summary=_fetcher(summary),
            runtime_policy=POLICY,
            open_sleeper_url="https://sleeper.com",
            poll_id="wake-2",
        )

        assert result.outcome == "unavailable"
        assert result.evaluation is not None
        assert result.evaluation.reason_codes == ("mapping_unavailable",)
        assert len(sender.messages) == 2
        assert all(message.title.startswith("Lock-In unavailable") for message in sender.messages)

    asyncio.run(exercise())


def test_actionable_delivery_failure_is_reported_for_retry(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Do not report a successful postgame attempt when delivery failed."""

    async def exercise() -> None:
        repository, inputs, notifications, sender = await _prepare(tmp_path)
        summary = _summary_result(_box("401", points=20), _box("402", points=8))
        await run_postgame_lock_in(
            "g1",
            inputs,
            decision_time=POSTGAME,
            repository=repository,
            notifications=notifications,
            fetch_summary=_fetcher(summary),
            runtime_policy=POLICY,
            open_sleeper_url="https://sleeper.com",
            poll_id="wake-1",
        )
        sender.fail = True
        notifications._clock = lambda: SECOND_POLL

        result = await run_postgame_lock_in(
            "g1",
            inputs,
            decision_time=SECOND_POLL,
            repository=repository,
            notifications=notifications,
            fetch_summary=_fetcher(summary),
            runtime_policy=POLICY,
            open_sleeper_url="https://sleeper.com",
            poll_id="wake-2",
        )

        assert result.outcome == "delivery_failed"
        assert result.recommendation is not None
        assert len(sender.messages) == 1

    asyncio.run(exercise())


def test_scoring_bonuses_and_dnps_use_discovered_policy(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Apply Sleeper bonuses and still treat a DNP row as a present player."""

    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        inputs = _live_inputs(scoring=ScoringPolicy(points=1, bonus_50_points=5))
        await sync_live_lock_in_opportunities(inputs, repository=repository, observed_at=NOW)
        sender = RecordingSender()
        notifications = NotificationLoop(
            repository,
            NotificationDispatcher(sender),
            acknowledgement_base_url="https://example.test/ack",
            clock=lambda: POSTGAME,
            acknowledgement_kinds=LOCK_IN_ACKNOWLEDGEMENT_KINDS,
        )
        summary = _summary_result(
            _box("401", points=50),
            _box("402", did_play=False),
        )
        await run_postgame_lock_in(
            "g1",
            inputs,
            decision_time=POSTGAME,
            repository=repository,
            notifications=notifications,
            fetch_summary=_fetcher(summary),
            runtime_policy=POLICY,
            open_sleeper_url="https://sleeper.com",
            poll_id="wake-1",
        )
        notifications._clock = lambda: SECOND_POLL
        await run_postgame_lock_in(
            "g1",
            inputs,
            decision_time=SECOND_POLL,
            repository=repository,
            notifications=notifications,
            fetch_summary=_fetcher(summary),
            runtime_policy=POLICY,
            open_sleeper_url="https://sleeper.com",
            poll_id="wake-2",
        )
        stored = await repository.get_lock_in_opportunity(
            LockInOpportunityKey("league-1", 1, 1, "p1", "g1")
        )
        dnp = await repository.get_lock_in_opportunity(
            LockInOpportunityKey("league-1", 1, 1, "p2", "g1")
        )
        assert stored is not None and stored.stable_score == 55.0
        assert dnp is not None and dnp.stable_score == 0.0

    asyncio.run(exercise())


def test_final_eligible_game_is_automatic_without_notification(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Persist the last eligible score without sending Lock or Pass advice."""

    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        inputs = _inputs(
            schedule_results=(_schedule_result(_game("g1", start=GAME_START)),),
            projections=(
                LiveProjectionResult("p1", "g1", _expected_snapshot("p1", "g1", 20.0), None),
                LiveProjectionResult("p2", "g1", _expected_snapshot("p2", "g1", 8.0), None),
            ),
            freshness_policy=_freshness_policy(
                max_nba_schedule_age=timedelta(days=2),
                max_availability_age=timedelta(days=2),
                max_sleeper_age=timedelta(days=2),
            ),
        )
        await sync_live_lock_in_opportunities(inputs, repository=repository, observed_at=NOW)
        sender = RecordingSender()
        notifications = NotificationLoop(
            repository,
            NotificationDispatcher(sender),
            acknowledgement_base_url="https://example.test/ack",
            clock=lambda: POSTGAME,
            acknowledgement_kinds=LOCK_IN_ACKNOWLEDGEMENT_KINDS,
        )
        summary = _summary_result(_box("401", points=20), _box("402", points=8))
        await run_postgame_lock_in(
            "g1",
            inputs,
            decision_time=POSTGAME,
            repository=repository,
            notifications=notifications,
            fetch_summary=_fetcher(summary),
            runtime_policy=POLICY,
            open_sleeper_url="https://sleeper.com",
            poll_id="wake-1",
        )
        notifications._clock = lambda: SECOND_POLL
        result = await run_postgame_lock_in(
            "g1",
            inputs,
            decision_time=SECOND_POLL,
            repository=repository,
            notifications=notifications,
            fetch_summary=_fetcher(summary),
            runtime_policy=POLICY,
            open_sleeper_url="https://sleeper.com",
            poll_id="wake-2",
        )
        stored = await repository.get_lock_in_opportunity(
            LockInOpportunityKey("league-1", 1, 1, "p1", "g1")
        )
        assert result.outcome == "automatic_final"
        assert stored is not None
        assert stored.status is LockInOpportunityStatus.AUTOMATIC_FINAL
        assert stored.stable_score == 20.0
        assert sender.messages == []

    asyncio.run(exercise())
