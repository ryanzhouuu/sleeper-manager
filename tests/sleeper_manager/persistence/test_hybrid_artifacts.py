"""Hybrid artifacts reject corruption, oversized decoding, and fabricated coverage."""

from dataclasses import asdict, replace
from datetime import timedelta

import pytest

from sleeper_manager.persistence.hybrid_artifacts import (
    MAX_HYBRID_ARTIFACT_BYTES,
    decode_hybrid_artifact,
    encode_hybrid_artifact,
)
from sleeper_manager.projections.hybrid_config import HybridProjectionError
from sleeper_manager.projections.hybrid_runtime_codec import (
    HybridHistoryManifest,
    decode_history_manifest,
    decode_player_partition,
    decode_runtime_index,
    history_manifest_version,
)
from sleeper_manager.projections.hybrid_runtime_index import build_hybrid_runtime_index
from tests.sleeper_manager.projections.hybrid_support import CUTOFF, TIPOFF, history
from tests.sleeper_manager.projections.test_hybrid_runtime_index import POLICY


def test_index_partition_and_manifest_round_trip_with_deterministic_bytes() -> None:
    data = history()
    index = build_hybrid_runtime_index(
        data, reference_time=CUTOFF, game_start=TIPOFF, scoring_policy=POLICY
    )
    encoded = encode_hybrid_artifact(index)
    assert encoded == encode_hybrid_artifact(index)
    assert decode_runtime_index(decode_hybrid_artifact(encoded)) == index
    rows = tuple(row for row in data.observations if row.player_id == "target")
    partition = encode_hybrid_artifact(rows)
    assert decode_player_partition(decode_hybrid_artifact(partition), "target") == rows
    parts = (("target", partition.artifact_id),)
    manifest = HybridHistoryManifest(
        history_manifest_version(encoded.artifact_id, parts),
        data.dataset_version,
        CUTOFF,
        encoded.artifact_id,
        parts,
    )
    assert (
        decode_history_manifest(decode_hybrid_artifact(encode_hybrid_artifact(manifest)))
        == manifest
    )
    changed = replace(index, reference_time=CUTOFF + timedelta(seconds=1))
    assert encode_hybrid_artifact(changed).artifact_id != encoded.artifact_id


@pytest.mark.parametrize(
    "field,value",
    [
        ("content_hash", "wrong"),
        ("artifact_id", "wrong"),
        ("encoded_payload", "corrupt"),
        ("uncompressed_size", 1),
        ("uncompressed_size", MAX_HYBRID_ARTIFACT_BYTES + 1),
        ("uncompressed_size", True),
    ],
)
def test_corrupt_or_unbounded_artifacts_are_rejected(field: str, value: object) -> None:
    encoded = encode_hybrid_artifact({"evidence": "retained"})
    with pytest.raises(HybridProjectionError):
        decode_hybrid_artifact(replace(encoded, **{field: value}))


def test_partition_requires_source_values_and_matching_player() -> None:
    row = asdict(history().observations[0])
    row["line"].pop("points")
    decoded = decode_hybrid_artifact(encode_hybrid_artifact([row]))
    with pytest.raises(HybridProjectionError, match="invalid_player_partition"):
        decode_player_partition(decoded, "target")
    valid = decode_hybrid_artifact(encode_hybrid_artifact((history().observations[0],)))
    with pytest.raises(HybridProjectionError, match="invalid_player_partition"):
        decode_player_partition(valid, "other")


def test_unknown_nested_index_fields_are_rejected() -> None:
    index = build_hybrid_runtime_index(
        history(), reference_time=CUTOFF, game_start=TIPOFF, scoring_policy=POLICY
    )
    raw = decode_hybrid_artifact(encode_hybrid_artifact(index))
    raw["profiles"][0]["invented"] = 1
    with pytest.raises(HybridProjectionError):
        decode_runtime_index(raw)
