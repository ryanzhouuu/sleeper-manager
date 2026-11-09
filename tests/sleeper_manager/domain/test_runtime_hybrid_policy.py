"""Hybrid advice requires pinned evidence; removed shadow evaluation cannot be re-enabled."""

import json
from dataclasses import replace

import pytest

from sleeper_manager.domain.runtime_policy import (
    RuntimePolicy,
    RuntimePolicyError,
    default_runtime_policy,
)


def test_existing_runtime_envelopes_default_to_baseline() -> None:
    policy = default_runtime_policy(history_version="control-history")
    raw = json.loads(policy.to_json())
    for key in ("hybrid_history_version", "active_projection_provider"):
        raw.pop(key)
    loaded = RuntimePolicy.from_json(policy.version, json.dumps(raw))
    assert loaded == policy
    assert loaded.active_projection_provider == "direct_baseline"


def test_stored_envelopes_with_shadow_disabled_still_load() -> None:
    policy = replace(
        default_runtime_policy(history_version="control-history"),
        hybrid_history_version="hybrid-runtime-hash",
        active_projection_provider="hybrid",
    )
    raw = json.loads(policy.to_json())
    assert "hybrid_shadow_enabled" not in raw
    raw["hybrid_shadow_enabled"] = False
    assert RuntimePolicy.from_json(policy.version, json.dumps(raw)) == policy


@pytest.mark.parametrize(
    "values",
    [
        {"hybrid_shadow_enabled": True},
        {"hybrid_shadow_enabled": "false"},
        {"hybrid_history_version": ""},
        {"hybrid_history_version": 3},
        {"active_projection_provider": "hybrid"},
    ],
)
def test_invalid_activation_or_promotion_is_rejected(values) -> None:  # type: ignore[no-untyped-def]
    raw = json.loads(default_runtime_policy(history_version="control").to_json())
    raw.update(values)
    with pytest.raises(RuntimePolicyError):
        RuntimePolicy.from_json("policy", json.dumps(raw))
