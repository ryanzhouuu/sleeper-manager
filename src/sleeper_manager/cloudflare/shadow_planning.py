"""Pair live forecasts and select advice only through the explicit runtime policy."""

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import fields, replace
from datetime import datetime

from sleeper_manager.cloudflare.active_hybrid import apply_active_hybrid
from sleeper_manager.cloudflare.providers import CloudflareSleeperClient
from sleeper_manager.cloudflare.stage_logging import timed_stage
from sleeper_manager.domain.league import LeagueProfile
from sleeper_manager.domain.runtime_policy import RuntimePolicy
from sleeper_manager.persistence.forecast_repository import (
    AsyncForecastArchiveRepository,
    ForecastArchiveIntegrityError,
)
from sleeper_manager.persistence.hybrid_artifacts import encode_hybrid_artifact
from sleeper_manager.persistence.hybrid_runtime import HybridRuntimeRepository
from sleeper_manager.projections.hybrid_config import HybridProjectionError
from sleeper_manager.projections.hybrid_model import HybridProjectionResult
from sleeper_manager.projections.hybrid_runtime_provider import HybridRuntimeProjectionProvider
from sleeper_manager.workflows.forecast_tipoffs import opponent_roster_id, parse_matchup_sides
from sleeper_manager.workflows.planning_collection import CollectedLiveEvidence
from sleeper_manager.workflows.planning_inputs import LiveProjectionProvider
from sleeper_manager.workflows.planning_projections import project_live_targets

SHADOW_TIMEOUT_SECONDS = 60


async def finish_hybrid_shadow_planning(
    manager: CollectedLiveEvidence,
    *,
    sleeper: CloudflareSleeperClient,
    collect_opponent: Callable[[LeagueProfile], Awaitable[CollectedLiveEvidence]],
    control: LiveProjectionProvider,
    policy: RuntimePolicy,
    store: HybridRuntimeRepository | None,
    archive: AsyncForecastArchiveRepository | None,
    clock: Callable[[], datetime],
) -> CollectedLiveEvidence:
    """Use one candidate batch for selected advice and optional durable paired evaluation."""
    profile = manager.inputs.league_profile
    evidence = [manager]
    warnings = list(manager.warnings)
    try:
        if policy.hybrid_shadow_enabled:
            with timed_stage("opponent_evidence"):
                sides = parse_matchup_sides(
                    await sleeper.matchups(profile.league_id, profile.fantasy_week.week)
                )
                opponent_id = opponent_roster_id(sides, profile.manager_roster_id)
                if opponent_id is not None:
                    evidence.append(
                        await collect_opponent(replace(profile, manager_roster_id=opponent_id))
                    )
    except Exception as error:
        warnings.append(f"hybrid_shadow_opponent_gap:{type(error).__name__}")
    cutoff = clock()
    targets = tuple(
        {
            (target.sleeper_player_id, target.game_id): target
            for item in evidence
            for target in item.projection_targets
        }.values()
    )
    availability = {
        (player, report.game_id): report
        for item in evidence
        for player, report in item.game_availability
    }
    with timed_stage("baseline_projection"):
        controls = await project_live_targets(
            targets,
            projection_provider=control,
            scoring_policy=profile.scoring,
            decision_time=cutoff,
            availability=availability,
            max_availability_age=policy.availability_max_age,
            generic_availability={
                row.player_id: row
                for item in evidence
                for result in item.inputs.availability_results
                for row in result.records
            },
            availability_gap=next(
                (
                    warning
                    for warning in manager.warnings
                    if warning.startswith(
                        (
                            "official_availability_",
                            "stale_official_",
                            "future_official_",
                            "unverified_official_",
                        )
                    )
                ),
                None,
            ),
        )
    manager_roster = next(
        roster for roster in profile.rosters if roster.roster_id == profile.manager_roster_id
    )
    own = set(manager_roster.player_ids)
    updated = replace(
        manager,
        inputs=replace(
            manager.inputs,
            projections=tuple(row for row in controls if row.sleeper_player_id in own),
        ),
        decision_time=cutoff,
    )
    if policy.active_projection_provider == "hybrid":
        updated = await apply_active_hybrid(
            updated, (), policy=policy, cutoff=cutoff, availability=availability
        )
    future = tuple(target for target in targets if target.game_start > cutoff)
    if not future:
        return replace(updated, warnings=tuple(warnings))
    if store is None:
        warnings.append("hybrid_shadow_store_missing")
        _log_shadow("failed", len(future), failure="hybrid_shadow_store_missing")
        return replace(updated, warnings=tuple(warnings))
    assert policy.hybrid_history_version is not None
    candidate = HybridRuntimeProjectionProvider(
        store, policy.hybrid_history_version, archive=archive, stage=timed_stage
    )
    try:
        with timed_stage("hybrid_projection"):
            attempts = await asyncio.wait_for(
                candidate.project_batch(
                    future,
                    scoring_policy=profile.scoring,
                    decision_time=cutoff,
                    availability=availability,
                ),
                timeout=SHADOW_TIMEOUT_SECONDS,
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
    if policy.active_projection_provider == "hybrid":
        updated = await apply_active_hybrid(
            updated, attempts, policy=policy, cutoff=cutoff, availability=availability
        )
    if not policy.hybrid_shadow_enabled:
        return replace(updated, decision_time=clock(), warnings=tuple(warnings))
    try:
        with timed_stage("hybrid_shadow_persistence"):
            control_by_key = {(row.sleeper_player_id, row.game_id): row for row in controls}
            pairs = []
            excluded: dict[int, str] = {}
            for attempt in attempts:
                exclusion_id = excluded.get(id(attempt.excluded_games))
                if exclusion_id is None:
                    exclusion_id = await store.save_value(attempt.excluded_games)
                    excluded[id(attempt.excluded_games)] = exclusion_id
                value = {
                    field.name: getattr(attempt, field.name)
                    for field in fields(attempt)
                    if field.name not in ("joint_weights", "excluded_games")
                }
                value["joint_weights_artifact_id"] = await store.save_value(attempt.joint_weights)
                value["excluded_games_artifact_id"] = exclusion_id
                key = attempt.target.sleeper_player_id, attempt.target.game_id
                baseline = control_by_key[key]
                delta = (
                    attempt.projection.distribution.expected_value
                    - baseline.snapshot.distribution.expected_value
                    if attempt.projection is not None and baseline.snapshot is not None
                    else None
                )
                pairs.append(
                    await store.save_value(
                        {"control": baseline, "candidate": value, "expected_value_delta": delta}
                    )
                )
            persisted_at = clock()
            manifest = {
                "schema_version": "live-hybrid-shadow-v1",
                "league_id": profile.league_id,
                "cutoff": cutoff,
                "control_history_version": policy.projection_history_version,
                "active_projection_provider": policy.active_projection_provider,
                "hybrid_history_version": policy.hybrid_history_version,
                "model_version": candidate.config.model_version,
                "scoring_policy_version": profile.scoring.version,
                "pairs": pairs,
                "availability": tuple(
                    (player, report) for (player, _), report in sorted(availability.items())
                ),
                "rosters": tuple(
                    {
                        "roster_id": item.inputs.league_profile.manager_roster_id,
                        "identities": item.inputs.identities,
                        "eligibility": item.inputs.player_eligibility,
                        "schedules": item.inputs.schedule_results,
                        "warnings": item.warnings,
                    }
                    for item in evidence
                ),
                "warnings": tuple(warnings),
                "timing": "on_time"
                if all(persisted_at < target.game_start for target in future)
                else "late",
            }
            run_id = "hybrid-shadow-v1-" + encode_hybrid_artifact(manifest).content_hash
            await store.save_shadow_run(
                run_id, profile.league_id, cutoff, manifest, persisted_at=persisted_at
            )
            _log_shadow(
                "recorded",
                len(attempts),
                failures=sum(row.failure is not None for row in attempts),
                run_id=run_id,
            )
    except Exception as error:
        warnings.append(f"hybrid_shadow_persistence_failed:{type(error).__name__}")
        _log_shadow("failed", len(future), failure=f"persistence:{type(error).__name__}")
    return replace(updated, decision_time=clock(), warnings=tuple(warnings))


def _log_shadow(outcome: str, targets: int, **details: object) -> None:
    """Expose operational gaps without logging roster payloads or notification destinations."""
    print(
        json.dumps(
            {"event": "hybrid_shadow", "outcome": outcome, "targets": targets, **details},
            sort_keys=True,
        ),
        file=sys.stderr,
    )
