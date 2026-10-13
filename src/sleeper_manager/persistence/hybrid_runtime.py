"""Persist immutable hybrid history references and shadow evidence beside runtime state."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from sleeper_manager.persistence.d1 import D1StateRepository
from sleeper_manager.persistence.hybrid_artifacts import (
    HybridArtifact,
    decode_hybrid_artifact,
    encode_hybrid_artifact,
)
from sleeper_manager.projections.hybrid_config import HybridProjectionError
from sleeper_manager.projections.hybrid_runtime_codec import (
    HybridHistoryManifest,
    decode_census_partition,
    decode_history_manifest,
    decode_player_partition,
    decode_runtime_index,
    history_manifest_version,
)
from sleeper_manager.projections.hybrid_runtime_index import HybridRuntimeIndex
from sleeper_manager.projections.hybrid_types import JointGameObservation, require_aware

HYBRID_RUNTIME_SCHEMA = """
CREATE TABLE IF NOT EXISTS hybrid_artifacts (
    artifact_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    encoded_payload TEXT NOT NULL,
    uncompressed_size INTEGER NOT NULL CHECK(uncompressed_size >= 0)
);
CREATE TABLE IF NOT EXISTS hybrid_history_versions (
    version TEXT PRIMARY KEY,
    manifest_artifact_id TEXT NOT NULL REFERENCES hybrid_artifacts(artifact_id),
    reference_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hybrid_shadow_runs (
    run_id TEXT PRIMARY KEY,
    league_id TEXT NOT NULL,
    cutoff TEXT NOT NULL,
    manifest_artifact_id TEXT NOT NULL REFERENCES hybrid_artifacts(artifact_id)
);
CREATE INDEX IF NOT EXISTS hybrid_shadow_cutoffs ON hybrid_shadow_runs(league_id, cutoff);
"""


class HybridSQL(Protocol):
    """Use the existing async SQL result validation for D1 and the local SQLite adapter."""

    async def _first(self, query: str, *params: object) -> dict[str, Any] | None: ...
    async def _run(self, query: str, *params: object) -> Any: ...


class HybridRuntimeRepository:
    """Publish version/run manifests last; incomplete artifact uploads remain unreferenced."""

    def __init__(self, sql: HybridSQL) -> None:
        self._sql = sql

    async def save_artifact(self, artifact: HybridArtifact) -> str:
        """Idempotently store exact bytes and reject any immutable-identity conflict."""
        decode_hybrid_artifact(artifact)
        await self._sql._run(
            "INSERT OR IGNORE INTO hybrid_artifacts VALUES (?, ?, ?, ?)",
            artifact.artifact_id,
            artifact.content_hash,
            artifact.encoded_payload,
            artifact.uncompressed_size,
        )
        loaded = await self._artifact(artifact.artifact_id)
        if loaded != artifact:
            raise HybridProjectionError("hybrid_artifact_conflict")
        return artifact.artifact_id

    async def save_value(self, value: object) -> str:
        return await self.save_artifact(encode_hybrid_artifact(value))

    async def _artifact(self, artifact_id: str) -> HybridArtifact:
        """Missing referenced bytes are an integrity failure, never ordinary absent forecasts."""
        row = await self._sql._first(
            "SELECT * FROM hybrid_artifacts WHERE artifact_id = ?", artifact_id
        )
        if row is None:
            raise HybridProjectionError("hybrid_artifact_missing")
        if not all(
            isinstance(row[key], str) for key in ("artifact_id", "content_hash", "encoded_payload")
        ) or not isinstance(row["uncompressed_size"], int):
            raise HybridProjectionError("invalid_hybrid_artifact_row")
        return HybridArtifact(
            row["artifact_id"],
            row["content_hash"],
            row["encoded_payload"],
            row["uncompressed_size"],
        )

    async def load_value(self, artifact_id: str) -> object:
        return decode_hybrid_artifact(await self._artifact(artifact_id))

    async def publish_history(self, manifest: HybridHistoryManifest) -> None:
        """Verify all referenced partitions before publishing a selectable immutable version."""
        if manifest.version != history_manifest_version(
            manifest.index_artifact_id, manifest.player_artifacts, manifest.census_artifacts
        ):
            raise HybridProjectionError("hybrid_manifest_identity_mismatch")
        index = decode_runtime_index(await self.load_value(manifest.index_artifact_id))
        if (index.dataset_version, index.reference_time) != (
            manifest.dataset_version,
            manifest.reference_time,
        ):
            raise HybridProjectionError("hybrid_manifest_index_mismatch")
        if {row.player_id for row in index.profiles} - {
            player for player, _ in manifest.player_artifacts
        }:
            raise HybridProjectionError("hybrid_manifest_missing_profile_partition")
        for player, artifact_id in manifest.player_artifacts:
            decode_player_partition(await self.load_value(artifact_id), player)
        if index.participation and not manifest.census_artifacts:
            raise HybridProjectionError("hybrid_census_artifacts_missing")
        for artifact_id in manifest.census_artifacts:
            decode_census_partition(await self.load_value(artifact_id))
        artifact_id = await self.save_value(manifest)
        await self._sql._run(
            "INSERT OR IGNORE INTO hybrid_history_versions VALUES (?, ?, ?)",
            manifest.version,
            artifact_id,
            manifest.reference_time.astimezone(UTC).isoformat(),
        )
        row = await self._sql._first(
            "SELECT * FROM hybrid_history_versions WHERE version = ?", manifest.version
        )
        if row is None or row["manifest_artifact_id"] != artifact_id:
            raise HybridProjectionError("hybrid_history_version_conflict")

    async def load_history(
        self,
        version: str,
        *,
        cutoff: datetime,
    ) -> tuple[HybridHistoryManifest, HybridRuntimeIndex]:
        """Require a pinned complete history version available by the decision cutoff."""
        require_aware(cutoff)
        row = await self._sql._first(
            "SELECT * FROM hybrid_history_versions WHERE version = ?", version
        )
        if row is None:
            raise HybridProjectionError("hybrid_history_version_missing")
        manifest = decode_history_manifest(await self.load_value(row["manifest_artifact_id"]))
        if manifest.version != version or version != history_manifest_version(
            manifest.index_artifact_id, manifest.player_artifacts, manifest.census_artifacts
        ):
            raise HybridProjectionError("hybrid_manifest_identity_mismatch")
        if manifest.reference_time > cutoff:
            raise HybridProjectionError("runtime_index_after_cutoff")
        if datetime.fromisoformat(row["reference_time"]) != manifest.reference_time:
            raise HybridProjectionError("hybrid_manifest_index_mismatch")
        index = decode_runtime_index(await self.load_value(manifest.index_artifact_id))
        if (index.dataset_version, index.reference_time) != (
            manifest.dataset_version,
            manifest.reference_time,
        ):
            raise HybridProjectionError("hybrid_manifest_index_mismatch")
        if {row.player_id for row in index.profiles} - {
            player for player, _ in manifest.player_artifacts
        }:
            raise HybridProjectionError("hybrid_manifest_missing_profile_partition")
        return manifest, index

    async def load_player_games(
        self,
        manifest: HybridHistoryManifest,
        player_id: str,
    ) -> tuple[JointGameObservation, ...]:
        """Only absence from the verified manifest represents genuine no-history evidence."""
        artifact_id = dict(manifest.player_artifacts).get(player_id)
        if artifact_id is None:
            return ()
        return decode_player_partition(await self.load_value(artifact_id), player_id)

    async def save_shadow_run(
        self,
        run_id: str,
        league_id: str,
        cutoff: datetime,
        manifest: Mapping[str, object],
    ) -> str:
        """Publish complete paired evidence atomically through its final immutable reference."""
        require_aware(cutoff)
        artifact_id = await self.save_value(manifest)
        await self._sql._run(
            "INSERT OR IGNORE INTO hybrid_shadow_runs VALUES (?, ?, ?, ?)",
            run_id,
            league_id,
            cutoff.astimezone(UTC).isoformat(),
            artifact_id,
        )
        row = await self._sql._first("SELECT * FROM hybrid_shadow_runs WHERE run_id = ?", run_id)
        if (
            row is None
            or row["manifest_artifact_id"] != artifact_id
            or row["league_id"] != league_id
            or datetime.fromisoformat(row["cutoff"]) != cutoff
        ):
            raise HybridProjectionError("hybrid_shadow_run_conflict")
        return artifact_id


class D1HybridRuntimeRepository(HybridRuntimeRepository):
    """Use the state binding; schema changes are applied by the normal D1 migration runner."""

    def __init__(self, database: Any) -> None:
        super().__init__(D1StateRepository(database))
