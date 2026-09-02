"""Low-level canonical hash verification for development checkpoint JSON artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sleeper_manager.backtesting.artifacts import canonical_json_bytes, sha256_bytes


def checkpoint_content_hash(payload: object) -> str:
    """Return the canonical SHA-256 digest for checkpoint fields excluding the hash."""
    return sha256_bytes(canonical_json_bytes(payload))


def load_hash_verified_json(path: Path) -> dict[str, Any]:
    """Read unique-key JSON and verify its digest before callers decode semantic fields."""
    payload = json.loads(path.read_text(), object_pairs_hook=_unique_object)
    if not isinstance(payload, dict):
        raise ValueError("Checkpoint payload must be a JSON object")
    content_hash = payload.get("content_hash")
    if not isinstance(content_hash, str):
        raise ValueError("Checkpoint content hash must be a string")
    canonical_payload = dict(payload)
    del canonical_payload["content_hash"]
    if content_hash != checkpoint_content_hash(canonical_payload):
        raise ValueError("Checkpoint content hash does not match")
    return payload


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON object keys hidden by ordinary decoding."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Checkpoint contains duplicate key {key!r}")
        result[key] = value
    return result


__all__ = ("checkpoint_content_hash", "load_hash_verified_json")
