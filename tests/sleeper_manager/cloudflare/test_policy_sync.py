"""Policy-only synchronization verifies evidence and preserves unrelated runtime state."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.cloudflare.policy_sync import RuntimePolicySyncError, sync_runtime_policy
from sleeper_manager.cloudflare.runtime_sync import (
    records_from_observations,
    sync_cloudflare_runtime_data,
)
from sleeper_manager.config import ManagerPolicy, load_manager_policy
from sleeper_manager.domain.runtime_policy import RuntimePolicy
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import RuntimePolicyRecord
from tests.sleeper_manager.cloudflare.test_live_hybrid_shadow import setup
from tests.sleeper_manager.cloudflare.test_runtime_sync import NOW, _observation


async def state_for(path):  # type: ignore[no-untyped-def]
    """Use real state and artifact repositories; the selected model data is independently seeded."""
    manager, _, _, store, current = await setup()
    state = AsyncSQLiteStateRepository(path)
    await state.initialize()
    await state.save_runtime_policy(RuntimePolicyRecord(current.version, current.to_json(), NOW))
    records = records_from_observations(
        (_observation(),), history_version=current.projection_history_version
    )
    await state.save_projection_observations(records.records)
    return state, store, current, manager.inputs.league_profile.scoring


@pytest.mark.parametrize("provider", ["direct_baseline", "hybrid"])
@pytest.mark.parametrize("shadow", [False, True])
def test_policy_only_apply_selects_modes_without_changing_history(
    tmp_path, provider, shadow
) -> None:  # type: ignore[no-untyped-def]
    """Every independent provider/evaluation combination preserves compact history exactly."""

    async def exercise() -> None:
        state, store, current, scoring = await state_for(tmp_path / "state.db")
        before = await state.load_projection_observations(current.projection_history_version)
        policy = ManagerPolicy(
            projections={
                "active_provider": provider,
                "shadow_enabled": shadow,
                "hybrid_history_version": current.hybrid_history_version,
            }
        )
        report = await sync_runtime_policy(
            policy, state=state, store=store, scoring_policy=scoring, apply=True, now=NOW
        )
        record = await state.load_runtime_policy()
        loaded = RuntimePolicy.from_json(record.version, record.payload_json)
        assert loaded.active_projection_provider == provider
        assert loaded.hybrid_shadow_enabled == shadow
        assert loaded.hybrid_history_version == current.hybrid_history_version
        assert report["policy_activated"] is True
        assert loaded.version != current.version
        assert (
            await state.load_projection_observations(current.projection_history_version) == before
        )

    asyncio.run(exercise())


def test_policy_only_dry_run_verifies_without_mutating_policy(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The displayed dry-run selection is verified against real published artifacts."""

    async def exercise() -> None:
        state, store, current, scoring = await state_for(tmp_path / "state.db")
        before = await state.load_runtime_policy()
        report = await sync_runtime_policy(
            ManagerPolicy(
                projections={
                    "active_provider": "hybrid",
                    "hybrid_history_version": current.hybrid_history_version,
                }
            ),
            state=state,
            store=store,
            scoring_policy=scoring,
            apply=False,
            now=NOW,
        )
        assert report["profile_count"] > 0
        assert report["active_projection_provider"] == "hybrid"
        assert report["policy_activated"] is False
        assert await state.load_runtime_policy() == before

    asyncio.run(exercise())


def test_absent_projection_table_preserves_current_selection_and_preferences_update(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """Older preference files cannot accidentally disable evaluation or switch providers."""

    async def exercise() -> None:
        state, store, current, scoring = await state_for(tmp_path / "state.db")
        selected = replace(current, active_projection_provider="hybrid")
        await state.save_runtime_policy(
            RuntimePolicyRecord(selected.version, selected.to_json(), NOW)
        )
        manager = ManagerPolicy(decision={"preset": "conservative", "minimum_confidence": 0.85})
        await sync_runtime_policy(
            manager, state=state, store=store, scoring_policy=scoring, apply=True, now=NOW
        )
        record = await state.load_runtime_policy()
        loaded = RuntimePolicy.from_json(record.version, record.payload_json)
        assert loaded.active_projection_provider == "hybrid"
        assert loaded.hybrid_shadow_enabled
        assert loaded.manager_intent.minimum_confidence == 0.85

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "failure",
    [
        "missing_history",
        "wrong_scoring",
        "wrong_season",
        "missing_store",
        "empty_profiles",
        "concurrent_change",
    ],
)
def test_policy_verification_failure_preserves_existing_policy(tmp_path, failure) -> None:  # type: ignore[no-untyped-def]
    """No rejected evidence or concurrent change is overwritten by activation."""

    async def exercise() -> None:
        state, store, current, scoring = await state_for(tmp_path / "state.db")
        before = await state.load_runtime_policy()
        version = "missing" if failure == "missing_history" else current.hybrid_history_version
        manager = ManagerPolicy(
            projections={
                "active_provider": "hybrid",
                "shadow_enabled": True,
                "hybrid_history_version": version,
            }
        )
        selected_scoring = (
            replace(scoring, points=scoring.points + 1) if failure == "wrong_scoring" else scoring
        )
        selected_now = NOW + timedelta(days=365) if failure == "wrong_season" else NOW
        if failure == "empty_profiles":
            original = store.load_history

            async def empty(*args, **kwargs):  # type: ignore[no-untyped-def]
                manifest, index = await original(*args, **kwargs)
                return manifest, replace(index, profiles=())

            store.load_history = empty
        original_state_load = state.load_runtime_policy
        if failure == "concurrent_change":
            calls = 0

            async def changed():  # type: ignore[no-untyped-def]
                nonlocal calls
                calls += 1
                return before if calls == 1 else replace(before, version="other-operator")

            state.load_runtime_policy = changed
        with pytest.raises(ValueError):
            await sync_runtime_policy(
                manager,
                state=state,
                store=None if failure == "missing_store" else store,
                scoring_policy=selected_scoring,
                apply=True,
                now=selected_now,
            )
        assert await original_state_load() == before

    asyncio.run(exercise())


def test_policy_only_requires_existing_runtime_state(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Initial history activation remains a separate explicit operation."""

    async def exercise() -> None:
        state = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await state.initialize()
        with pytest.raises(RuntimePolicySyncError, match="Initialize"):
            await sync_runtime_policy(
                ManagerPolicy(),
                state=state,
                store=None,
                scoring_policy=ScoringPolicy(),
                apply=True,
                now=NOW,
            )
        assert await state.load_runtime_policy() is None

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "settings",
    [
        'active_provider = "hybrid"',
        "shadow_enabled = true",
        'active_provider = "unknown"',
        'shadow_enabled = "false"',
        'hybrid_history_version = ""',
        "unexpected = true",
    ],
)
def test_toml_projection_settings_reject_unpinned_or_invalid_selection(tmp_path, settings) -> None:  # type: ignore[no-untyped-def]
    """Invalid settings cannot enter the runtime envelope through local TOML."""
    path = tmp_path / "policy.toml"
    path.write_text("[projections]\n" + settings + "\n")
    with pytest.raises(ValueError):
        load_manager_policy(path)


def test_toml_projection_table_is_loaded_and_can_explicitly_disable_shadow(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """An explicit false value differs from an omitted legacy projection table."""
    path = tmp_path / "policy.toml"
    path.write_text('[projections]\nactive_provider = "direct_baseline"\nshadow_enabled = false\n')
    manager = load_manager_policy(path)
    assert manager.projections is not None
    assert manager.projections.shadow_enabled is False
    assert ManagerPolicy().projections is None


def test_policy_cli_never_creates_a_missing_local_database(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A policy-only dry run cannot initialize history or create runtime state."""
    from sleeper_manager.cloudflare.policy_sync_cli import parser, run

    target = tmp_path / "missing.db"
    args = parser().parse_args(["--local-db", str(target)])
    with pytest.raises(ValueError, match="Initialize"):
        asyncio.run(run(args))
    assert not target.exists()


def test_full_history_sync_cannot_bypass_new_hybrid_selection_verification(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The older initialization command must enforce the same selection gate."""

    async def exercise() -> None:
        state, _, current, _ = await state_for(tmp_path / "state.db")
        before = await state.load_runtime_policy()
        history = records_from_observations((_observation(),), history_version="not-uploaded")
        selected = replace(current, active_projection_provider="hybrid")
        with pytest.raises(RuntimePolicySyncError):
            await sync_cloudflare_runtime_data(
                history,
                policy=selected,
                repository=state,
                apply=True,
                now=NOW,
                preserve_projection_selection=False,
            )
        assert await state.load_runtime_policy() == before
        assert await state.load_projection_observations("not-uploaded") == ()

    asyncio.run(exercise())
