"""Shadow activation requires pinned evidence and cannot accidentally promote hybrid advice."""

import json
from dataclasses import replace

import pytest

from sleeper_manager.domain.runtime_policy import (
    RuntimePolicy,
    RuntimePolicyError,
    default_runtime_policy,
)


def test_existing_runtime_envelopes_default_to_baseline_without_shadow() -> None:
    policy = default_runtime_policy(history_version="control-history")
    raw = json.loads(policy.to_json())
    for key in ("hybrid_history_version", "hybrid_shadow_enabled", "active_projection_provider"):
        raw.pop(key)
    loaded = RuntimePolicy.from_json(policy.version, json.dumps(raw))
    assert loaded == policy
    assert loaded.active_projection_provider == "direct_baseline"
    assert not loaded.hybrid_shadow_enabled


def test_shadow_policy_round_trip_keeps_active_provider_separate() -> None:
    policy = replace(
        default_runtime_policy(history_version="control-history"),
        hybrid_history_version="hybrid-runtime-hash",
        hybrid_shadow_enabled=True,
    )
    assert RuntimePolicy.from_json(policy.version, policy.to_json()) == policy
    assert policy.active_projection_provider == "direct_baseline"


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
