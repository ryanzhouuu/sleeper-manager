"""Async archive-backed hybrid execution using compact profiles and bounded exact outcome reads."""

import asyncio
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.domain.projection import ProjectionSnapshot
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.sleeper.forecast_fetch import season_forecast_source
from sleeper_manager.persistence.forecast_repository import AsyncForecastArchiveRepository
from sleeper_manager.projections.hybrid_config import HybridProjectionConfig, HybridProjectionError
from sleeper_manager.projections.hybrid_external import qualify_external_center
from sleeper_manager.projections.hybrid_model import HybridProjectionBatch, HybridProjectionResult
from sleeper_manager.projections.hybrid_participation import HybridParticipation
from sleeper_manager.projections.hybrid_partition_cache import HybridPartitionCache
from sleeper_manager.projections.hybrid_runtime_codec import HybridHistoryManifest
from sleeper_manager.projections.hybrid_runtime_index import HybridRuntimeIndex, IndexedHistoryPools
from sleeper_manager.projections.hybrid_types import (
    GameAvailability,
    JointGameObservation,
    require_aware,
)
from sleeper_manager.projections.live_baseline import LiveProjectionTarget
from sleeper_manager.workflows.forecast_reader import read_player_forecasts_async


class HybridHistoryReader(Protocol):
    """Read a pinned verified index and separately addressed player outcomes."""

    async def load_history(
        self, version: str, *, cutoff: datetime
    ) -> tuple[HybridHistoryManifest, HybridRuntimeIndex]: ...
    async def load_player_games(
        self, manifest: HybridHistoryManifest, player_id: str
    ) -> tuple[JointGameObservation, ...]: ...


class HybridRuntimeProjectionProvider:
    """Retain every attempt with bounded batch reuse of selected player and donor outcomes."""

    def __init__(
        self,
        history: HybridHistoryReader,
        history_version: str,
        *,
        archive: AsyncForecastArchiveRepository | None = None,
        config: HybridProjectionConfig | None = None,
    ) -> None:
        self.history, self.history_version, self.archive = history, history_version, archive
        self.config = config or HybridProjectionConfig()
        self.results: tuple[HybridProjectionResult, ...] = ()

    async def project_live_batch(
        self,
        targets: tuple[LiveProjectionTarget, ...],
        *,
        scoring_policy: ScoringPolicy,
        decision_time: datetime,
        availability: Mapping[tuple[str, str], GameAvailability],
    ) -> tuple[ProjectionSnapshot | None, ...]:
        """Implement the live batch seam without discarding candidate failures and diagnostics."""
        self.results = await self.project_batch(
            targets,
            scoring_policy=scoring_policy,
            decision_time=decision_time,
            availability=availability,
        )
        return tuple(row.projection for row in self.results)

    async def project_batch(
        self,
        targets: tuple[LiveProjectionTarget, ...],
        *,
        scoring_policy: ScoringPolicy,
        decision_time: datetime,
        availability: Mapping[tuple[str, str], GameAvailability] | None = None,
    ) -> tuple[HybridProjectionResult, ...]:
        """Share revision and population preparation; fetch outcomes by player group."""
        require_aware(decision_time)
        keys = tuple((target.sleeper_player_id, target.game_id) for target in targets)
        if len(set(keys)) != len(keys):
            raise HybridProjectionError("duplicate_projection_target")
        if not targets:
            return ()
        manifest, index = await self.history.load_history(
            self.history_version, cutoff=decision_time
        )
        if scoring_policy.version != index.scoring_policy_version:
            raise HybridProjectionError("runtime_index_scoring_mismatch")
        population = index.population(decision_time, self.config)
        participation = HybridParticipation.from_counts(
            dict(index.participation), cutoff=decision_time, config=self.config
        )
        reads = {}
        if self.archive is not None and self.config.use_external:
            source = season_forecast_source(str(index.season), "regular")
            reads = {
                row.player_id: row
                for row in await read_player_forecasts_async(
                    self.archive,
                    source,
                    tuple(sorted({target.sleeper_player_id for target in targets})),
                    decision_time,
                )
            }
        groups: dict[tuple[str | None, str], list[LiveProjectionTarget]] = defaultdict(list)
        for target in targets:
            groups[target.provider_player_id, target.sleeper_player_id].append(target)
        results = {}
        partitions = HybridPartitionCache()
        for (player, sleeper_id), group in groups.items():
            needed = {player} if player is not None else set()
            if player is not None:
                external = qualify_external_center(
                    reads.get(sleeper_id),
                    player_id=sleeper_id,
                    cutoff=decision_time,
                    game_start=group[0].game_start,
                    config=self.config,
                )
                for center in (None, external.values):
                    try:
                        needed.update(population.donors(player, center))
                    except HybridProjectionError:
                        pass
            observations = await self._load_partitions(
                manifest, tuple(sorted(needed)), cache=partitions
            )
            pools = IndexedHistoryPools(
                index,
                population,
                observations,
                cutoff=decision_time,
                scoring_policy=scoring_policy,
                config=self.config,
            )
            prepared = HybridProjectionBatch.from_prepared(
                cutoff=decision_time,
                season=index.season,
                scoring_policy=scoring_policy,
                config=self.config,
                pools=pools,
                participation=participation,
                history_fingerprint=index.history_fingerprint,
            )
            for target in group:
                key = target.sleeper_player_id, target.game_id
                results[key] = (
                    HybridProjectionResult(
                        target,
                        decision_time,
                        failure="runtime_index_season_mismatch",
                        history_fingerprint=index.history_fingerprint,
                    )
                    if nba_season_start_year(target.game_start) != index.season
                    else prepared.project(
                        target,
                        forecast=reads.get(sleeper_id),
                        availability=(availability or {}).get(key),
                    )
                )
        return tuple(results[key] for key in keys)

    async def _load_partitions(
        self,
        manifest: HybridHistoryManifest,
        players: tuple[str, ...],
        *,
        cache: HybridPartitionCache,
    ) -> dict[str, tuple[JointGameObservation, ...]]:
        """Bound D1 read concurrency and wait for every read before propagating a failure."""
        observations = {
            player: cached for player in players if (cached := cache.get(player)) is not None
        }
        missing = tuple(player for player in players if player not in observations)
        for offset in range(0, len(missing), 6):
            group = missing[offset : offset + 6]
            rows = await asyncio.gather(
                *(self.history.load_player_games(manifest, player) for player in group),
                return_exceptions=True,
            )
            for player, result in zip(group, rows, strict=True):
                if isinstance(result, BaseException):
                    raise result
                observations[player] = result
                cache.remember(player, result)
        return observations
