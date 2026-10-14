"""Verify pinned projection evidence before changing the active D1 policy.

Policy-only updates reuse existing history and do not deploy code or rebuild data.
The full history sync shares the hybrid validation boundary below.
"""

from dataclasses import replace
from datetime import datetime
from hashlib import sha256

from sleeper_manager.config import ManagerPolicy
from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.domain.runtime_policy import RuntimePolicy
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository, RuntimePolicyRecord
from sleeper_manager.persistence.hybrid_runtime import HybridRuntimeRepository
from sleeper_manager.projections.hybrid_config import HybridProjectionConfig
from sleeper_manager.projections.hybrid_types import require_aware


class RuntimePolicySyncError(ValueError):
    """The requested policy cannot be activated against the verified runtime evidence."""


def select_runtime_policy(current: RuntimePolicy, manager: ManagerPolicy) -> RuntimePolicy:
    """Absent projection settings preserve selection; an explicit table replaces it."""
    policy = current
    if manager.projections is not None:
        policy = replace(
            policy,
            active_projection_provider=manager.projections.active_provider,
            hybrid_shadow_enabled=manager.projections.shadow_enabled,
            hybrid_history_version=manager.projections.hybrid_history_version,
        )
    policy = replace(
        policy,
        manager_intent=manager.to_manager_intent(),
        mapping_overrides=dict(manager.players.mapping_overrides),
    )
    return version_runtime_policy(policy)


def version_runtime_policy(policy: RuntimePolicy) -> RuntimePolicy:
    """Bind identity to the final combined policy after preserved fields are resolved."""
    version = "runtime-policy-v2-" + sha256(policy.to_json().encode()).hexdigest()[:12]
    return replace(policy, version=version)


async def verify_hybrid_policy(
    policy: RuntimePolicy,
    *,
    store: HybridRuntimeRepository | None,
    scoring_policy: ScoringPolicy | None,
    now: datetime,
) -> dict[str, object]:
    """Reject unavailable, unscorable, wrong-season, or incompatible prepared history."""
    require_aware(now)
    if not policy.hybrid_shadow_enabled and policy.active_projection_provider != "hybrid":
        return {}
    if store is None or scoring_policy is None or policy.hybrid_history_version is None:
        raise RuntimePolicySyncError("Hybrid selection requires history and scoring verification")
    manifest, index = await store.load_history(policy.hybrid_history_version, cutoff=now)
    if index.scoring_policy_version != scoring_policy.version:
        raise RuntimePolicySyncError("Hybrid history uses a different scoring policy")
    if index.model_version != HybridProjectionConfig().model_version:
        raise RuntimePolicySyncError("Hybrid history uses a different model version")
    if index.season != nba_season_start_year(now.date()):
        raise RuntimePolicySyncError("Hybrid history uses a different NBA season")
    if not index.profiles:
        raise RuntimePolicySyncError("Hybrid history has no eligible full-scoring profiles")
    return {
        "hybrid_history_version": manifest.version,
        "model_version": index.model_version,
        "scoring_policy_version": index.scoring_policy_version,
        "profile_count": len(index.profiles),
        "excluded_games": len(index.excluded_games),
    }


async def sync_runtime_policy(
    manager: ManagerPolicy,
    *,
    state: AsyncRuntimeStateRepository,
    store: HybridRuntimeRepository | None,
    scoring_policy: ScoringPolicy,
    apply: bool,
    now: datetime,
) -> dict[str, object]:
    """Dry-run reads and verifies; apply changes only policy after a concurrent-change check."""
    require_aware(now)
    existing = await state.load_runtime_policy()
    if existing is None:
        raise RuntimePolicySyncError(
            "Initialize runtime history and policy before a policy-only sync"
        )
    current = RuntimePolicy.from_json(existing.version, existing.payload_json)
    policy = select_runtime_policy(current, manager)
    evidence = await verify_hybrid_policy(
        policy, store=store, scoring_policy=scoring_policy, now=now
    )
    if policy.active_projection_provider == "direct_baseline":
        baseline = await state.load_projection_observations(
            policy.projection_history_version, before=now
        )
        if not baseline:
            raise RuntimePolicySyncError("Selected baseline history has no observations")
    report: dict[str, object] = {
        "apply": apply,
        "policy_activated": False,
        "policy_version": policy.version,
        "active_projection_provider": policy.active_projection_provider,
        "hybrid_shadow_enabled": policy.hybrid_shadow_enabled,
        "projection_history_version": policy.projection_history_version,
        **evidence,
    }
    if apply:
        if await state.load_runtime_policy() != existing:
            raise RuntimePolicySyncError("Runtime policy changed during verification; retry")
        record = RuntimePolicyRecord(policy.version, policy.to_json(), now)
        await state.save_runtime_policy(record)
        if await state.load_runtime_policy() != record:
            raise RuntimePolicySyncError("Saved runtime policy did not verify")
        report["policy_activated"] = True
    return report
