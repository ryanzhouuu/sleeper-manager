"""D1 and SQLite preserve immutable history and shadow references through failures."""

import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from sleeper_manager.persistence.hybrid_runtime import (
    HYBRID_RUNTIME_SCHEMA,
    D1HybridRuntimeRepository,
)
from sleeper_manager.persistence.hybrid_sqlite import SQLiteHybridRuntimeRepository
from sleeper_manager.projections.hybrid_config import HybridProjectionError
from sleeper_manager.projections.hybrid_runtime_codec import (
    HybridHistoryManifest,
    history_manifest_version,
)
from sleeper_manager.projections.hybrid_runtime_index import build_hybrid_runtime_index
from tests.sleeper_manager.persistence.test_d1 import FakeD1
from tests.sleeper_manager.projections.hybrid_support import CUTOFF, TIPOFF, history
from tests.sleeper_manager.projections.test_hybrid_runtime_index import POLICY


@pytest.fixture(params=["d1", "sqlite"])
def storage(request, tmp_path):  # type: ignore[no-untyped-def]
    if request.param == "d1":
        database = FakeD1()
        asyncio.run(database.exec(HYBRID_RUNTIME_SCHEMA))
        return D1HybridRuntimeRepository(database), database.connection
    path = tmp_path / "runtime.db"
    repository = SQLiteHybridRuntimeRepository(path)
    asyncio.run(repository.initialize())
    return repository, sqlite3.connect(path)


async def save_history(repository):  # type: ignore[no-untyped-def]
    data = history()
    index = build_hybrid_runtime_index(
        data, reference_time=CUTOFF, game_start=TIPOFF, scoring_policy=POLICY
    )
    index_id = await repository.save_value(index)
    partitions = []
    for player in sorted({row.player_id for row in data.observations}):
        artifact_id = await repository.save_value(
            tuple(row for row in data.observations if row.player_id == player)
        )
        partitions.append((player, artifact_id))
    parts = tuple(partitions)
    census = (await repository.save_value(data.opportunities),)
    manifest = HybridHistoryManifest(
        history_manifest_version(index_id, parts, census),
        data.dataset_version,
        CUTOFF,
        index_id,
        parts,
        census,
    )
    await repository.publish_history(manifest)
    return manifest, index


def test_history_round_trip_is_cutoff_safe_and_missing_player_is_explicit(storage) -> None:  # type: ignore[no-untyped-def]
    repository, _ = storage
    manifest, index = asyncio.run(save_history(repository))
    assert asyncio.run(repository.load_history(manifest.version, cutoff=CUTOFF)) == (
        manifest,
        index,
    )
    rows = asyncio.run(repository.load_player_games(manifest, "target"))
    assert len(rows) == 30 and all(row.player_id == "target" for row in rows)
    assert asyncio.run(repository.load_player_games(manifest, "unknown")) == ()
    with pytest.raises(HybridProjectionError, match="after_cutoff"):
        asyncio.run(repository.load_history(manifest.version, cutoff=CUTOFF - timedelta(seconds=1)))


def test_missing_partition_prevents_publishing_selectable_history(storage) -> None:  # type: ignore[no-untyped-def]
    repository, connection = storage
    index = build_hybrid_runtime_index(
        history(), reference_time=CUTOFF, game_start=TIPOFF, scoring_policy=POLICY
    )
    index_id = asyncio.run(repository.save_value(index))
    parts = tuple((row.player_id, "missing") for row in index.profiles)
    manifest = HybridHistoryManifest(
        history_manifest_version(index_id, parts), index.dataset_version, CUTOFF, index_id, parts
    )
    with pytest.raises(HybridProjectionError, match="artifact_missing"):
        asyncio.run(repository.publish_history(manifest))
    assert connection.execute("SELECT count(*) FROM hybrid_history_versions").fetchone()[0] == 0


def test_corrupt_partition_is_not_treated_as_no_history(storage) -> None:  # type: ignore[no-untyped-def]
    repository, connection = storage
    manifest, _ = asyncio.run(save_history(repository))
    partition_id = dict(manifest.player_artifacts)["target"]
    connection.execute(
        "UPDATE hybrid_artifacts SET encoded_payload = 'corrupt' WHERE artifact_id = ?",
        (partition_id,),
    )
    connection.commit()
    with pytest.raises(HybridProjectionError, match="invalid_hybrid_artifact"):
        asyncio.run(repository.load_player_games(manifest, "target"))


def test_shadow_run_reference_is_immutable_and_retains_failure_evidence(storage) -> None:  # type: ignore[no-untyped-def]
    repository, connection = storage
    value = {"candidate_failure": "unverified_player_stats", "control": None}
    first = asyncio.run(repository.save_shadow_run("run", "league", CUTOFF, value))
    assert asyncio.run(repository.save_shadow_run("run", "league", CUTOFF, value)) == first
    assert asyncio.run(repository.load_value(first)) == value
    with pytest.raises(HybridProjectionError, match="shadow_run_conflict"):
        asyncio.run(
            repository.save_shadow_run("run", "league", CUTOFF, {"candidate_failure": None})
        )
    assert connection.execute("SELECT count(*) FROM hybrid_shadow_runs").fetchone()[0] == 1


def test_runtime_schema_matches_the_deploy_migration() -> None:
    root = Path(__file__).resolve().parents[3]
    path = root / "infra/cloudflare/migrations/0005_hybrid_runtime.sql"
    assert path.read_text().strip() == HYBRID_RUNTIME_SCHEMA.strip()
