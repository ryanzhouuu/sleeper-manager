"""Project live targets with the pinned hybrid history when policy selects hybrid advice."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from sleeper_manager.cloudflare.active_hybrid import apply_active_hybrid
from sleeper_manager.cloudflare.hybrid_projection_cache import ForecastReader, project_with_cache
from sleeper_manager.cloudflare.stage_logging import timed_stage
from sleeper_manager.domain.forecast_capture import ForecastRetrievalResult
from sleeper_manager.domain.runtime_policy import RuntimePolicy
from sleeper_manager.integrations.sleeper.forecast_fetch import season_forecast_source
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository
from sleeper_manager.persistence.forecast_repository import (
    AsyncForecastArchiveRepository,
    ForecastArchiveIntegrityError,
)
from sleeper_manager.persistence.hybrid_runtime import HybridRuntimeRepository
from sleeper_manager.projections.hybrid_config import HybridProjectionError
from sleeper_manager.projections.hybrid_model import HybridProjectionResult
from sleeper_manager.projections.hybrid_runtime_provider import HybridRuntimeProjectionProvider
from sleeper_manager.workflows.forecast_reader import read_player_forecasts_async
from sleeper_manager.workflows.planning_collection import CollectedLiveEvidence

HYBRID_TIMEOUT_SECONDS = 60


async def finish_hybrid_planning(
    manager: CollectedLiveEvidence,
    *,
    policy: RuntimePolicy,
    store: HybridRuntimeRepository | None,
    archive: AsyncForecastArchiveRepository | None,
    clock: Callable[[], datetime],
    timeout_seconds: float | None = None,
    cache: AsyncRuntimeStateRepository | None = None,
    timezone_name: str | None = None,
) -> CollectedLiveEvidence:
    """Failed or missing candidates stay explicit gaps; no baseline advice is substituted."""
    assert policy.hybrid_history_version is not None
    warnings = list(manager.warnings)
    availability = {
        (player, report.game_id): report for player, report in manager.game_availability
    }
    cutoff = clock()
    updated = await apply_active_hybrid(
        replace(manager, decision_time=cutoff),
        (),
        policy=policy,
        cutoff=cutoff,
        availability=availability,
    )
    future = tuple(target for target in manager.projection_targets if target.game_start > cutoff)
    if not future:
        return updated
    if store is None:
        warnings.append("hybrid_store_missing")
        return replace(updated, warnings=tuple(warnings))
    candidate = HybridRuntimeProjectionProvider(
        store, policy.hybrid_history_version, archive=archive, stage=timed_stage
    )
    try:
        with timed_stage("hybrid_projection"):
            scoring = manager.inputs.league_profile.scoring
            batch = (
                candidate.project_batch(
                    future, scoring_policy=scoring, decision_time=cutoff, availability=availability
                )
                if cache is None or timezone_name is None
                else project_with_cache(
                    candidate,
                    future,
                    cache=cache,
                    scoring_policy=scoring,
                    decision_time=cutoff,
                    availability=availability,
                    read_forecasts=_forecast_reader(archive),
                    timezone_name=timezone_name,
                )
            )
            attempts = await asyncio.wait_for(
                batch,
                timeout=HYBRID_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds,
            )
    except Exception as error:
        failure = (
            error.reason
            if isinstance(error, HybridProjectionError)
            else "forecast_archive_integrity_error"
            if isinstance(error, ForecastArchiveIntegrityError)
            else f"candidate_exception:{type(error).__name__}"
        )
        attempts = tuple(
            HybridProjectionResult(target, cutoff, failure=failure) for target in future
        )
    if archive is None:
        warnings.append("hybrid_forecast_archive_missing")
    updated = await apply_active_hybrid(
        updated, attempts, policy=policy, cutoff=cutoff, availability=availability
    )
    return replace(updated, decision_time=clock(), warnings=tuple(warnings))


def _forecast_reader(archive: AsyncForecastArchiveRepository | None) -> ForecastReader | None:
    if archive is None:
        return None

    async def read(
        season: int, players: tuple[str, ...], cutoff: datetime
    ) -> dict[str, ForecastRetrievalResult]:
        source = season_forecast_source(str(season), "regular")
        rows = await read_player_forecasts_async(archive, source, players, cutoff)
        return {row.player_id: row for row in rows}

    return read
