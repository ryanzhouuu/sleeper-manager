from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any


def canonicalize(value: object) -> Any:
    if isinstance(value, Enum):
        return canonicalize(value.value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if is_dataclass(value):
        return {field.name: canonicalize(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {
            str(key): canonicalize(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (tuple, list)):
        return [canonicalize(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def canonical_json(value: object) -> str:
    return json.dumps(canonicalize(value), sort_keys=True, separators=(",", ":"))


def canonical_json_bytes(value: object) -> bytes:
    return canonical_json(value).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode())


def atomic_write_bytes(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as temp:
        temp.write(payload)
        temp.flush()
        os.fsync(temp.fileno())
        temporary_path = Path(temp.name)
    try:
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return sha256_bytes(payload)


def atomic_write_json(path: Path, payload: object) -> str:
    return atomic_write_bytes(path, canonical_json_bytes(payload))


__all__ = (
    "atomic_write_bytes",
    "atomic_write_json",
    "canonical_json",
    "canonical_json_bytes",
    "canonicalize",
    "sha256_bytes",
    "sha256_text",
)
