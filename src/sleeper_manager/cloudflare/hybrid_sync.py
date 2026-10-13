"""Prepare complete hybrid evidence offline and publish verified runtime history before policy."""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256

from sleeper_manager.domain.runtime_policy import RuntimePolicy
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository, RuntimePolicyRecord
from sleeper_manager.persistence.hybrid_artifacts import HybridArtifact, encode_hybrid_artifact
from sleeper_manager.persistence.hybrid_runtime import HybridRuntimeRepository
from sleeper_manager.projections.hybrid_config import HybridProjectionConfig, HybridProjectionError
from sleeper_manager.projections.hybrid_runtime_codec import (
    HybridHistoryManifest,
    history_manifest_version,
)
from sleeper_manager.projections.hybrid_runtime_index import (
    HybridRuntimeIndex,
    build_hybrid_runtime_index,
)
from sleeper_manager.projections.hybrid_types import (
    HybridHistory,
    JointGameObservation,
    require_aware,
)


@dataclass(frozen=True, slots=True)
class HybridRuntimeExport:
    """Keep original stat and census evidence alongside the compact preparation."""

    manifest: HybridHistoryManifest
    index: HybridRuntimeIndex
    artifacts: tuple[HybridArtifact, ...]
    observation_count: int
    opportunity_count: int
    status_tagged_count: int


def build_hybrid_runtime_export(
    history: HybridHistory,
    *,
    reference_time: datetime,
    game_start: datetime,
    scoring_policy: ScoringPolicy,
    config: HybridProjectionConfig | None = None,
) -> HybridRuntimeExport:
    """Partition exact player outcomes and census rows without thinning either evidence source."""
    index = build_hybrid_runtime_index(
        history,
        reference_time=reference_time,
        game_start=game_start,
        scoring_policy=scoring_policy,
        config=config,
    )
    index_artifact = encode_hybrid_artifact(index)
    artifacts = {index_artifact.artifact_id: index_artifact}
    rows: dict[str, list[JointGameObservation]] = defaultdict(list)
    for row in sorted(history.observations, key=lambda row: (row.player_id, row.game_id)):
        rows[row.player_id].append(row)
    partitions = []
    for player, observations in sorted(rows.items()):
        artifact = encode_hybrid_artifact(observations)
        artifacts[artifact.artifact_id] = artifact
        partitions.append((player, artifact.artifact_id))
    opportunities = sorted(history.opportunities, key=lambda row: (row.player_id, row.game_id))
    census = []
    for offset in range(0, len(opportunities), 1000):
        artifact = encode_hybrid_artifact(opportunities[offset : offset + 1000])
        artifacts[artifact.artifact_id] = artifact
        census.append(artifact.artifact_id)
    parts, census_ids = tuple(partitions), tuple(census)
    manifest = HybridHistoryManifest(
        history_manifest_version(index_artifact.artifact_id, parts, census_ids),
        history.dataset_version,
        reference_time,
        index_artifact.artifact_id,
        parts,
        census_ids,
    )
    return HybridRuntimeExport(
        manifest,
        index,
        tuple(artifacts.values()),
        len(history.observations),
        len(opportunities),
        sum(row.availability is not None for row in opportunities),
    )


async def sync_hybrid_runtime_history(
    export: HybridRuntimeExport,
    *,
    store: HybridRuntimeRepository | None,
    state: AsyncRuntimeStateRepository | None,
    apply: bool,
    enable_shadow: bool,
    now: datetime,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    """Dry-run by default; activate shadow only after exact artifact and index verification."""
    require_aware(now)
    report: dict[str, object] = {
        "apply": apply,
        "history_version": export.manifest.version,
        "dataset_version": export.manifest.dataset_version,
        "model_version": export.index.model_version,
        "scoring_policy_version": export.index.scoring_policy_version,
        "observation_count": export.observation_count,
        "opportunity_count": export.opportunity_count,
        "status_tagged_count": export.status_tagged_count,
        "artifact_count": len(export.artifacts),
        "encoded_bytes": sum(len(row.encoded_payload) for row in export.artifacts),
        "profile_count": len(export.index.profiles),
        "excluded_games": len(export.index.excluded_games),
        "enable_shadow": enable_shadow,
        "shadow_enabled": False,
        "active_projection_provider": "direct_baseline",
    }
    if not apply:
        return report
    if store is None:
        raise HybridProjectionError("hybrid_sync_store_missing")
    policy = None
    if enable_shadow:
        record = await state.load_runtime_policy() if state is not None else None
        if record is None:
            raise HybridProjectionError("hybrid_shadow_requires_existing_runtime_policy")
        policy = RuntimePolicy.from_json(record.version, record.payload_json)
    for artifact in export.artifacts:
        await store.save_artifact(artifact)
    tick = clock or (lambda: datetime.now(UTC))
    published_at = await store.publish_history(export.manifest, published_at=tick(), clock=tick)
    manifest, index = await store.load_history(export.manifest.version, cutoff=published_at)
    if manifest != export.manifest or index != export.index:
        raise HybridProjectionError("hybrid_sync_verification_failed")
    if policy is not None:
        assert state is not None
        updated = replace(
            policy, hybrid_history_version=manifest.version, hybrid_shadow_enabled=True
        )
        version = "runtime-policy-v2-" + sha256(updated.to_json().encode()).hexdigest()[:12]
        updated = replace(updated, version=version)
        await state.save_runtime_policy(RuntimePolicyRecord(version, updated.to_json(), tick()))
        report["shadow_enabled"] = True
        report["policy_version"] = version
    return report
