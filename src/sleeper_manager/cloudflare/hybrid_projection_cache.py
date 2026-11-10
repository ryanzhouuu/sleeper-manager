"""Reuse hybrid projections for the rest of the manager-local day while their evidence holds.

The model's recency decay moves outputs slightly as the cutoff advances. Within one day that
drift is accepted, so repeated passes reuse identical snapshots instead of reloading history
partitions, and an unchanged team state can skip its replan. Any change to the forecast center,
a fresh official report, the history version, model config, scoring, or the target recomputes.
Cache reads and writes that fail are misses; they never block projection.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from hashlib import sha256
from typing import Protocol

from sleeper_manager.cloudflare.home_cache import read_home_cache, write_home_cache
from sleeper_manager.domain.forecast_capture import ForecastRetrievalResult
from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.domain.projection import (
    ProjectionDistribution,
    ProjectionReason,
    ProjectionSnapshot,
)
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository
from sleeper_manager.projections.hybrid_config import HybridProjectionConfig
from sleeper_manager.projections.hybrid_external import qualify_external_center
from sleeper_manager.projections.hybrid_model import HybridProjectionResult
from sleeper_manager.projections.hybrid_participation import availability_gap
from sleeper_manager.projections.hybrid_types import GameAvailability
from sleeper_manager.projections.live_baseline import LiveProjectionTarget

PROJECTION_CACHE_RESOURCE = "hybrid-projection"
_SCHEMA = "1"
_READ_CONCURRENCY = 6
_CACHEABLE_SOURCES = frozenset({"internal", "external"})

ForecastReader = Callable[
    [int, tuple[str, ...], datetime], Awaitable[Mapping[str, ForecastRetrievalResult]]
]


class HybridBatchProjector(Protocol):
    history_version: str
    config: HybridProjectionConfig

    async def project_batch(
        self,
        targets: tuple[LiveProjectionTarget, ...],
        *,
        scoring_policy: ScoringPolicy,
        decision_time: datetime,
        availability: Mapping[tuple[str, str], GameAvailability] | None = None,
    ) -> tuple[HybridProjectionResult, ...]: ...


async def project_with_cache(
    candidate: HybridBatchProjector,
    targets: tuple[LiveProjectionTarget, ...],
    *,
    cache: AsyncRuntimeStateRepository,
    scoring_policy: ScoringPolicy,
    decision_time: datetime,
    availability: Mapping[tuple[str, str], GameAvailability],
    read_forecasts: ForecastReader | None,
    timezone_name: str,
) -> tuple[HybridProjectionResult, ...]:
    """Project only targets without a same-day snapshot computed from identical evidence."""
    forecasts = await _forecasts(candidate.config, targets, decision_time, read_forecasts)
    evidence = {
        _key(target): _evidence_hash(
            candidate,
            target,
            scoring_policy=scoring_policy,
            forecast=forecasts.get(target.sleeper_player_id),
            availability=availability.get(_key(target)),
            cutoff=decision_time,
        )
        for target in targets
    }
    cached: dict[tuple[str, str], ProjectionSnapshot] = {}
    for offset in range(0, len(targets), _READ_CONCURRENCY):
        group = targets[offset : offset + _READ_CONCURRENCY]
        snapshots = await asyncio.gather(
            *(
                _read(
                    cache, candidate.history_version, target, evidence[_key(target)], decision_time
                )
                for target in group
            )
        )
        cached.update(
            (_key(target), snapshot)
            for target, snapshot in zip(group, snapshots, strict=True)
            if snapshot is not None
        )
    misses = tuple(target for target in targets if _key(target) not in cached)
    computed = (
        await candidate.project_batch(
            misses,
            scoring_policy=scoring_policy,
            decision_time=decision_time,
            availability=availability,
        )
        if misses
        else ()
    )
    for result in computed:
        payload = _cacheable_payload(result, evidence[_key(result.target)])
        if payload is not None:
            await write_home_cache(
                cache,
                _cache_key(candidate.history_version, result.target),
                payload,
                resource=PROJECTION_CACHE_RESOURCE,
                timezone_name=timezone_name,
                now=decision_time,
            )
    fresh = {_key(result.target): result for result in computed}
    return tuple(
        fresh.get(_key(target))
        or HybridProjectionResult(
            target, decision_time, projection=cached[_key(target)], source="cached"
        )
        for target in targets
    )


async def _forecasts(
    config: HybridProjectionConfig,
    targets: tuple[LiveProjectionTarget, ...],
    cutoff: datetime,
    read_forecasts: ForecastReader | None,
) -> dict[str, ForecastRetrievalResult]:
    if read_forecasts is None or not config.use_external:
        return {}
    by_season: dict[int, set[str]] = {}
    for target in targets:
        by_season.setdefault(nba_season_start_year(target.game_start), set()).add(
            target.sleeper_player_id
        )
    forecasts: dict[str, ForecastRetrievalResult] = {}
    for season, players in sorted(by_season.items()):
        forecasts.update(await read_forecasts(season, tuple(sorted(players)), cutoff))
    return forecasts


def _evidence_hash(
    candidate: HybridBatchProjector,
    target: LiveProjectionTarget,
    *,
    scoring_policy: ScoringPolicy,
    forecast: ForecastRetrievalResult | None,
    availability: GameAvailability | None,
    cutoff: datetime,
) -> str:
    config = candidate.config
    external = qualify_external_center(
        forecast,
        player_id=target.sleeper_player_id,
        cutoff=cutoff,
        game_start=target.game_start,
        config=config,
    )
    gap = availability_gap(availability, target.game_id, cutoff=cutoff, config=config)
    report = (
        None
        if gap is not None or availability is None
        else [
            availability.status.value,
            availability.observation.value,
            availability.source_version,
            availability.confirmed_absence,
        ]
    )
    payload = {
        "schema": _SCHEMA,
        "history": candidate.history_version,
        "config": repr(config),
        "scoring": scoring_policy.version,
        "target": [
            target.sleeper_player_id,
            target.game_id,
            target.game_start.isoformat(),
            target.provider_player_id,
        ],
        "forecast": [
            external.values,
            external.rejection,
            forecast.newer_attempt_receipt_id if forecast is not None else None,
        ],
        "availability": report or gap,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


async def _read(
    cache: AsyncRuntimeStateRepository,
    history_version: str,
    target: LiveProjectionTarget,
    evidence: str,
    now: datetime,
) -> ProjectionSnapshot | None:
    payload = await read_home_cache(cache, _cache_key(history_version, target), now)
    if not isinstance(payload, dict) or payload.get("evidence") != evidence:
        return None
    try:
        snapshot = _decode_snapshot(payload["snapshot"])
    except (KeyError, TypeError, ValueError):
        return None
    if (snapshot.player_id, snapshot.game_id) != _key(target) or snapshot.available_as_of > now:
        return None
    return snapshot


def _cacheable_payload(result: HybridProjectionResult, evidence: str) -> dict[str, object] | None:
    snapshot = result.projection
    if (
        snapshot is None
        or result.failure is not None
        or result.source not in _CACHEABLE_SOURCES
        or snapshot.components
    ):
        return None
    return {"evidence": evidence, "snapshot": _encode_snapshot(snapshot)}


def _encode_snapshot(snapshot: ProjectionSnapshot) -> dict[str, object]:
    distribution = snapshot.distribution
    return {
        "player_id": snapshot.player_id,
        "game_id": snapshot.game_id,
        "available_as_of": snapshot.available_as_of.isoformat(),
        "model_version": snapshot.model_version,
        "input_version": snapshot.input_version,
        "scoring_policy_version": snapshot.scoring_policy_version,
        "distribution": {
            "expected_value": distribution.expected_value,
            "median": distribution.median,
            "percentiles": [list(item) for item in distribution.percentiles],
            "lower_bound": distribution.lower_bound,
            "upper_bound": distribution.upper_bound,
            "variance": distribution.variance,
            "weighted_observations": [list(item) for item in distribution.weighted_observations],
            "exceedance_score": distribution.exceedance_score,
            "probability_exceeding_score": distribution.probability_exceeding_score,
        },
        "reasons": [
            [reason.code, reason.message, reason.applied, reason.adjustment]
            for reason in snapshot.reasons
        ],
    }


def _decode_snapshot(raw: dict[str, object]) -> ProjectionSnapshot:
    distribution = raw["distribution"]
    reasons = raw["reasons"]
    if not isinstance(distribution, dict) or not isinstance(reasons, list):
        raise TypeError("Cached projection is malformed")
    available_as_of = datetime.fromisoformat(str(raw["available_as_of"]))
    if available_as_of.tzinfo is None:
        raise ValueError("Cached projection time must be timezone-aware")
    return ProjectionSnapshot(
        player_id=_text(raw["player_id"]),
        game_id=_text(raw["game_id"]),
        available_as_of=available_as_of,
        model_version=_text(raw["model_version"]),
        input_version=_text(raw["input_version"]),
        scoring_policy_version=_text(raw["scoring_policy_version"]),
        distribution=ProjectionDistribution(
            expected_value=_number(distribution["expected_value"]),
            median=_number(distribution["median"]),
            percentiles=tuple(
                (int(_number(percentile)), _number(value))
                for percentile, value in distribution["percentiles"]
            ),
            lower_bound=_number(distribution["lower_bound"]),
            upper_bound=_number(distribution["upper_bound"]),
            variance=_number(distribution["variance"]),
            weighted_observations=tuple(
                (_number(value), _number(weight))
                for value, weight in distribution["weighted_observations"]
            ),
            exceedance_score=_optional_number(distribution["exceedance_score"]),
            probability_exceeding_score=_optional_number(
                distribution["probability_exceeding_score"]
            ),
        ),
        reasons=tuple(
            ProjectionReason(
                _text(code), _text(message), bool(applied), _optional_number(adjustment)
            )
            for code, message, applied, adjustment in reasons
        ),
    )


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("Cached projection text field is malformed")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("Cached projection number is malformed")
    return float(value)


def _optional_number(value: object) -> float | None:
    return None if value is None else _number(value)


def _key(target: LiveProjectionTarget) -> tuple[str, str]:
    return target.sleeper_player_id, target.game_id


def _cache_key(history_version: str, target: LiveProjectionTarget) -> str:
    return (
        f"{PROJECTION_CACHE_RESOURCE}:{history_version}:{target.sleeper_player_id}:{target.game_id}"
    )


__all__ = ("PROJECTION_CACHE_RESOURCE", "ForecastReader", "project_with_cache")
