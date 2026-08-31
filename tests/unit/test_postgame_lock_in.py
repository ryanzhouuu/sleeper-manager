"""Direct ESPN postgame observation, stabilization, and live Lock-In advice."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

from test_notification_loop import RecordingSender
from test_planning_inputs import (
    NOW,
    _freshness_policy,
    _game,
    _inputs,
    _profile,
    _schedule_result,
    _snapshot,
)

from sleeper_manager.domain.lock_in import LockInEvaluationKind, LockInOpportunityStatus
from sleeper_manager.domain.nba import (
    DataQualityReport,
    DataQualityState,
    GameStatus,
    GameSummary,
    PlayerBoxScore,
    ProviderResult,
    SourceMetadata,
)
from sleeper_manager.domain.projection import ProjectionDistribution
from sleeper_manager.domain.runtime_policy import default_runtime_policy
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy
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

GAME_START = NOW + timedelta(hours=2)
POSTGAME = GAME_START + timedelta(hours=2)
SECOND_POLL = POSTGAME + timedelta(minutes=5)
LATER_START = NOW + timedelta(days=1)
POLICY = default_runtime_policy(history_version="history-v1")


def _expected_snapshot(player_id: str, game_id: str, expected: float):
    """Build one projection snapshot with a deterministic expected value."""

    base = _snapshot(player_id, game_id)
    return replace(
        base,
        distribution=ProjectionDistribution(
            expected_value=expected,
            median=expected,
            percentiles=((50, expected),),
            lower_bound=expected,
            upper_bound=expected,
            variance=0,
        ),
    )


def _live_inputs(*, scoring: ScoringPolicy | None = None):
    """Return two-game live inputs with projections that admit a clear Lock."""

    profile = _profile() if scoring is None else replace(_profile(), scoring=scoring)
    first = _game("g1", start=GAME_START)
    second = _game("g2", start=LATER_START)
    return _inputs(
        profile,
        schedule_results=(_schedule_result(first, second),),
        projections=(
            LiveProjectionResult("p1", "g1", _expected_snapshot("p1", "g1", 20.0), None),
            LiveProjectionResult("p1", "g2", _expected_snapshot("p1", "g2", 1.0), None),
            LiveProjectionResult("p2", "g1", _expected_snapshot("p2", "g1", 8.0), None),
            LiveProjectionResult("p2", "g2", _expected_snapshot("p2", "g2", 8.0), None),
        ),
        freshness_policy=_freshness_policy(
            max_nba_schedule_age=timedelta(days=2),
            max_availability_age=timedelta(days=2),
            max_sleeper_age=timedelta(days=2),
        ),
    )


def _box(
    player_id: str,
    *,
    points: int = 0,
    rebounds: int = 0,
    did_play: bool = True,
    retrieved_at=POSTGAME,
) -> PlayerBoxScore:
    """Build one ESPN box-score row whose retrieval time is excluded from fingerprints."""

    return PlayerBoxScore(
        game_id="g1",
        player_id=player_id,
        team_id="12",
        played_at=GAME_START,
        started=True,
        did_play=did_play,
        minutes=30.0 if did_play else 0.0,
        line=BoxScoreLine(points=points, rebounds=rebounds),
        source=SourceMetadata(provider="espn", provider_id=player_id, retrieved_at=retrieved_at),
    )


def _summary_result(
    *boxes: PlayerBoxScore,
    status: GameStatus = GameStatus.FINAL,
    quality: DataQualityState = DataQualityState.FRESH,
    retrieved_at=POSTGAME,
) -> ProviderResult[GameSummary]:
    """Return one direct provider summary used as a scheduled postgame poll."""

    game = replace(
        _game("g1", start=GAME_START, status=status),
        finalized_at=POSTGAME if status is GameStatus.FINAL else None,
        source=SourceMetadata(provider="espn", provider_id="g1", retrieved_at=retrieved_at),
    )
    return ProviderResult(
        GameSummary(game, boxes),
        DataQualityReport(
            state=quality,
            resource="espn:game-summary:g1",
            record_count=len(boxes),
            retrieved_at=retrieved_at,
            source_updated_at=None,
            expires_at=None,
        ),
    )


def _fetcher(result: ProviderResult[GameSummary]):
    """Return a direct summary source that records each distinct scheduled wake."""

    calls: list[str] = []

    async def fetch(game_id: str) -> ProviderResult[GameSummary]:
        calls.append(game_id)
        return result

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


async def _prepare(tmp_path):  # type: ignore[no-untyped-def]
    """Persist pre-tipoff evidence and a notification loop for postgame wakes."""

    repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
    await repository.initialize()
    inputs = _live_inputs()
    await sync_live_lock_in_opportunities(inputs, repository=repository, observed_at=NOW)
    sender = RecordingSender()
    notifications = NotificationLoop(
        repository,
        NotificationDispatcher(sender),
        acknowledgement_base_url="https://example.test/ack",
        clock=lambda: POSTGAME,
        acknowledgement_kinds=LOCK_IN_ACKNOWLEDGEMENT_KINDS,
    )
    return repository, inputs, notifications, sender


async def _run_poll(tmp_path, result, *, at=POSTGAME, poll_id="wake-1"):  # type: ignore[no-untyped-def]
    """Run one postgame wake against a prepared opportunity set."""

    repository, inputs, notifications, sender = await _prepare(tmp_path)
    outcome = await run_postgame_lock_in(
        "g1",
        inputs,
        decision_time=at,
        repository=repository,
        notifications=notifications,
        fetch_summary=_fetcher(result),
        runtime_policy=POLICY,
        open_sleeper_url="https://sleeper.com",
        poll_id=poll_id,
        player_names={"p1": "Ann", "p2": "Ben"},
    )
    return repository, sender, outcome


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
