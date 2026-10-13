"""Bounded, content-addressed JSON artifacts for hybrid history and retained shadow traces."""

import base64
import gzip
import json
import zlib
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from hashlib import sha256

from sleeper_manager.projections.hybrid_config import HybridProjectionError

MAX_HYBRID_ARTIFACT_BYTES = 8_000_000
MAX_HYBRID_ENCODED_BYTES = 1_500_000


@dataclass(frozen=True, slots=True)
class HybridArtifact:
    """The identity binds decoded JSON bytes; compressed bytes are deterministic and bounded."""

    artifact_id: str
    content_hash: str
    encoded_payload: str
    uncompressed_size: int


def _json_default(value: object) -> object:
    """Encode domain records without permitting arbitrary object stringification."""
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"Unsupported hybrid artifact type: {type(value).__name__}")


def encode_hybrid_artifact(value: object) -> HybridArtifact:
    """Reject oversized artifacts before storage; callers partition large evidence explicitly."""
    raw = json.dumps(
        value, default=_json_default, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if len(raw) > MAX_HYBRID_ARTIFACT_BYTES:
        raise HybridProjectionError("hybrid_artifact_too_large")
    encoded = base64.b64encode(gzip.compress(raw, compresslevel=6, mtime=0)).decode()
    if len(encoded) > MAX_HYBRID_ENCODED_BYTES:
        raise HybridProjectionError("hybrid_artifact_too_large")
    digest = sha256(raw).hexdigest()
    return HybridArtifact(f"hybrid-json-v1-{digest}", digest, encoded, len(raw))


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Duplicate JSON keys cannot silently overwrite retained evidence."""
    if len(dict(pairs)) != len(pairs):
        raise HybridProjectionError("duplicate_hybrid_artifact_field")
    return dict(pairs)


def decode_hybrid_artifact(artifact: HybridArtifact) -> object:
    """Verify bounded decompression and content identity before exposing any parsed evidence."""
    if (
        not isinstance(artifact.uncompressed_size, int)
        or isinstance(artifact.uncompressed_size, bool)
        or not 0 <= artifact.uncompressed_size <= MAX_HYBRID_ARTIFACT_BYTES
        or len(artifact.encoded_payload) > MAX_HYBRID_ENCODED_BYTES
        or artifact.artifact_id != f"hybrid-json-v1-{artifact.content_hash}"
    ):
        raise HybridProjectionError("invalid_hybrid_artifact")
    try:
        compressed = base64.b64decode(artifact.encoded_payload, validate=True)
        decoder = zlib.decompressobj(wbits=31)
        raw = decoder.decompress(compressed, artifact.uncompressed_size + 1)
        if (
            len(raw) != artifact.uncompressed_size
            or not decoder.eof
            or decoder.unused_data
            or decoder.unconsumed_tail
            or sha256(raw).hexdigest() != artifact.content_hash
        ):
            raise HybridProjectionError("invalid_hybrid_artifact")
        return json.loads(raw, object_pairs_hook=_unique_fields)
    except (ValueError, zlib.error) as error:
        raise HybridProjectionError("invalid_hybrid_artifact") from error
