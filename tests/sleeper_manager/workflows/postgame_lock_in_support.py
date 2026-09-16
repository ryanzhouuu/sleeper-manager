"""Shared ESPN postgame fixtures for live Lock-In workflow tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

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
from sleeper_manager.workflows.lock_in_planning import sync_live_lock_in_opportunities
from sleeper_manager.workflows.notification_loop import NotificationLoop
from sleeper_manager.workflows.planning_inputs import LiveProjectionResult
from sleeper_manager.workflows.postgame_lock_in import (
    LOCK_IN_ACKNOWLEDGEMENT_KINDS,
    run_postgame_lock_in,
)
from tests.sleeper_manager.workflows.planning_inputs_support import (
    NOW,
    _freshness_policy,
    _game,
    _inputs,
    _profile,
    _schedule_result,
    _snapshot,
)
from tests.sleeper_manager.workflows.test_notification_loop import RecordingSender

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
