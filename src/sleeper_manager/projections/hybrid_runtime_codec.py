"""Strict indexed-history decoding; normalized defaults cannot prove source coverage."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter

from sleeper_manager.domain.scoring import BoxScoreLine
from sleeper_manager.persistence.hybrid_artifacts import encode_hybrid_artifact
from sleeper_manager.projections.hybrid_config import HybridProjectionError
from sleeper_manager.projections.hybrid_history_artifact import _shape
from sleeper_manager.projections.hybrid_participation_counts import ParticipationCounts
from sleeper_manager.projections.hybrid_runtime_index import (
    HybridRuntimeIndex,
    IndexedPlayerProfile,
)
from sleeper_manager.projections.hybrid_types import JointGameObservation, require_aware


@dataclass(frozen=True, slots=True)
class HybridHistoryManifest:
    """Bind one prepared index to immutable per-player outcome artifacts."""

    version: str
    dataset_version: str
    reference_time: datetime
    index_artifact_id: str
    player_artifacts: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        require_aware(self.reference_time)
        if not all((self.version, self.dataset_version, self.index_artifact_id)):
            raise HybridProjectionError("missing_hybrid_manifest_identity")
        if len({player for player, _ in self.player_artifacts}) != len(self.player_artifacts):
            raise HybridProjectionError("duplicate_hybrid_player_partition")


def _typed(value: object, kind: Any) -> Any:
    """Match the existing artifact loader's strict tuple and timestamp rules."""
    import json

    return TypeAdapter(kind).validate_json(json.dumps(value), strict=True)


def decode_runtime_index(value: object) -> HybridRuntimeIndex:
    """Reject unknown fields in every compact profile and census aggregate."""
    raw = _shape(value, HybridRuntimeIndex)
    for row in raw.get("profiles", []):
        _shape(row, IndexedPlayerProfile)
    for _, row in raw.get("participation", []):
        _shape(row, ParticipationCounts)
    result: HybridRuntimeIndex = _typed(raw, HybridRuntimeIndex)
    return result


def decode_history_manifest(value: object) -> HybridHistoryManifest:
    """Preserve the manifest identity instead of coercing partial storage records."""
    result: HybridHistoryManifest = _typed(
        _shape(value, HybridHistoryManifest), HybridHistoryManifest
    )
    return result


def decode_player_partition(value: object, player_id: str) -> tuple[JointGameObservation, ...]:
    """Require actual source values for declared coverage and only the requested player's rows."""
    if not isinstance(value, list):
        raise HybridProjectionError("invalid_player_partition")
    for item in value:
        row = _shape(item, JointGameObservation)
        line = _shape(row.get("line"), BoxScoreLine)
        if row.get("player_id") != player_id or not set(row.get("verified_fields", ())).issubset(
            line
        ):
            raise HybridProjectionError("invalid_player_partition")
    result: tuple[JointGameObservation, ...] = _typed(value, tuple[JointGameObservation, ...])
    if len({row.game_id for row in result}) != len(result):
        raise HybridProjectionError("duplicate_player_partition_game")
    return result


def history_manifest_version(index_id: str, partitions: tuple[tuple[str, str], ...]) -> str:
    """Bind preparation and exact outcomes through content-addressed references."""
    return "hybrid-runtime-v1-" + encode_hybrid_artifact((index_id, partitions)).content_hash
