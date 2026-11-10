"""Hybrid projections are reused within the manager-local day only while their evidence holds."""

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sleeper_manager.cloudflare.hybrid_projection_cache import (
    PROJECTION_CACHE_RESOURCE,
    project_with_cache,
)
from sleeper_manager.domain.forecast_capture import (
    ForecastRetrievalResult,
    ForecastRetrievalStatus,
)
from sleeper_manager.domain.nba import AvailabilityStatus
from sleeper_manager.domain.projection import (
    ProjectionDistribution,
    ProjectionReason,
    ProjectionSnapshot,
)
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_models import AvailabilityObservation
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import CachedNBARecord
from sleeper_manager.projections.hybrid_config import HybridProjectionConfig
from sleeper_manager.projections.hybrid_model import HybridProjectionResult
from sleeper_manager.projections.hybrid_types import GameAvailability
from sleeper_manager.projections.live_baseline import LiveProjectionTarget

NOW = datetime(2026, 11, 11, 1, 0, tzinfo=UTC)
TIMEZONE = "America/Chicago"
SCORING = ScoringPolicy()
TARGET = LiveProjectionTarget("p1", "g1", NOW + timedelta(days=1), "espn-p1")
OTHER = LiveProjectionTarget("p2", "g2", NOW + timedelta(days=1), "espn-p2")


class Candidate:
    def __init__(self, *, failure: str | None = None) -> None:
        self.history_version = "hybrid-v1"
        self.config = HybridProjectionConfig()
        self.failure = failure
        self.calls: list[tuple[str, ...]] = []

    async def project_batch(
        self,
        targets: tuple[LiveProjectionTarget, ...],
        *,
        scoring_policy: ScoringPolicy,
        decision_time: datetime,
        availability: Mapping[tuple[str, str], GameAvailability] | None = None,
    ) -> tuple[HybridProjectionResult, ...]:
        self.calls.append(tuple(target.sleeper_player_id for target in targets))
        if self.failure is not None:
            return tuple(
                HybridProjectionResult(target, decision_time, failure=self.failure)
                for target in targets
            )
        return tuple(
            HybridProjectionResult(
                target,
                decision_time,
                projection=_snapshot(target, decision_time),
                source="internal",
            )
            for target in targets
        )


def _snapshot(target: LiveProjectionTarget, cutoff: datetime) -> ProjectionSnapshot:
    return ProjectionSnapshot(
        target.sleeper_player_id,
        target.game_id,
        cutoff,
        "hybrid-model-v1",
        f"hybrid-input-v1-{cutoff.isoformat()}",
        SCORING.version,
        ProjectionDistribution.from_weighted_observations(((12.5, 0.25), (30.0, 0.5), (0.0, 0.25))),
        (ProjectionReason("joint_outcomes", "Whole historical stat lines."),),
    )


def _forecast(player_id: str, cutoff: datetime) -> ForecastRetrievalResult:
    return ForecastRetrievalResult(
        ForecastRetrievalStatus.GAP,
        player_id,
        cutoff,
        "capture failed",
        evidence_receipt_id="receipt-1",
    )


def _report(observed_at: datetime, status: AvailabilityStatus) -> GameAvailability:
    return GameAvailability(
        "g1", observed_at, status, AvailabilityObservation.REPORTED, "official-report-1"
    )


def _run(
    repository: AsyncSQLiteStateRepository,
    candidate: Candidate,
    *,
    at: datetime = NOW,
    targets: tuple[LiveProjectionTarget, ...] = (TARGET,),
    forecasts: bool = True,
    availability: Mapping[tuple[str, str], GameAvailability] | None = None,
) -> tuple[HybridProjectionResult, ...]:
    async def read_forecasts(
        season: int, players: tuple[str, ...], cutoff: datetime
    ) -> Mapping[str, ForecastRetrievalResult]:
        return {player: _forecast(player, cutoff) for player in players} if forecasts else {}

    return asyncio.run(
        project_with_cache(
            candidate,  # type: ignore[arg-type]
            targets,
            cache=repository,
            scoring_policy=SCORING,
            decision_time=at,
            availability=availability or {},
            read_forecasts=read_forecasts,
            timezone_name=TIMEZONE,
        )
    )


def _repository(tmp_path: Path) -> AsyncSQLiteStateRepository:
    repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
    asyncio.run(repository.initialize())
    return repository


def test_unchanged_evidence_reuses_the_identical_snapshot(tmp_path: Path) -> None:
    repository, candidate = _repository(tmp_path), Candidate()
    (first,) = _run(repository, candidate)
    (second,) = _run(repository, candidate, at=NOW + timedelta(hours=2))
    assert candidate.calls == [("p1",)]
    assert second.projection == first.projection
    assert second.failure is None


def test_only_missing_players_are_projected(tmp_path: Path) -> None:
    repository, candidate = _repository(tmp_path), Candidate()
    _run(repository, candidate)
    results = _run(repository, candidate, targets=(TARGET, OTHER))
    assert candidate.calls == [("p1",), ("p2",)]
    assert [row.target for row in results] == [TARGET, OTHER]
    assert all(row.projection is not None for row in results)


def test_changed_forecast_evidence_recomputes(tmp_path: Path) -> None:
    repository, candidate = _repository(tmp_path), Candidate()
    _run(repository, candidate)
    _run(repository, candidate, forecasts=False)
    assert candidate.calls == [("p1",), ("p1",)]


def test_fresh_official_report_recomputes_and_its_status_is_part_of_the_key(
    tmp_path: Path,
) -> None:
    repository, candidate = _repository(tmp_path), Candidate()
    _run(repository, candidate)
    questionable = {
        ("p1", "g1"): _report(NOW - timedelta(minutes=5), AvailabilityStatus.QUESTIONABLE)
    }
    _run(repository, candidate, availability=questionable)
    _run(repository, candidate, availability=questionable)
    out = {("p1", "g1"): _report(NOW - timedelta(minutes=5), AvailabilityStatus.DOUBTFUL)}
    _run(repository, candidate, availability=out)
    assert candidate.calls == [("p1",), ("p1",), ("p1",)]


def test_cache_expires_at_manager_local_midnight(tmp_path: Path) -> None:
    repository, candidate = _repository(tmp_path), Candidate()
    _run(repository, candidate)
    local_midnight = datetime(2026, 11, 11, 6, 0, tzinfo=UTC)
    _run(repository, candidate, at=local_midnight - timedelta(minutes=1))
    _run(repository, candidate, at=local_midnight)
    assert candidate.calls == [("p1",), ("p1",)]


def test_failed_projections_are_not_cached(tmp_path: Path) -> None:
    repository, candidate = _repository(tmp_path), Candidate(failure="insufficient_history")
    _run(repository, candidate)
    (result,) = _run(repository, candidate)
    assert candidate.calls == [("p1",), ("p1",)]
    assert result.failure == "insufficient_history"


def test_unreadable_cache_rows_are_misses(tmp_path: Path) -> None:
    repository, candidate = _repository(tmp_path), Candidate()
    _run(repository, candidate)
    key = "hybrid-projection:hybrid-v1:p1:g1"
    record: Any = asyncio.run(repository.get(key, now=NOW))
    assert record is not None and record.resource == PROJECTION_CACHE_RESOURCE
    asyncio.run(repository.put(replace(record, payload_json='{"evidence": 1}')))
    _run(repository, candidate)
    assert candidate.calls == [("p1",), ("p1",)]


def test_cache_read_failures_fall_back_to_projection(tmp_path: Path) -> None:
    class Broken(AsyncSQLiteStateRepository):
        async def get(self, cache_key: str, *, now: datetime) -> CachedNBARecord | None:
            raise OSError("d1 unavailable")

        async def put(self, record: CachedNBARecord) -> None:
            raise OSError("d1 unavailable")

    repository = Broken(tmp_path / "state.db")
    asyncio.run(repository.initialize())
    (result,) = _run(repository, Candidate())
    assert result.projection is not None
