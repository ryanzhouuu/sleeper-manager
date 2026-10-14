"""Selected hybrid advice fails closed independently of paired control diagnostics."""

import asyncio
from dataclasses import replace

import pytest

from sleeper_manager.cloudflare.shadow_planning import finish_hybrid_shadow_planning
from sleeper_manager.domain.runtime_policy import RuntimePolicy
from sleeper_manager.projections.hybrid_runtime_provider import HybridRuntimeProjectionProvider
from tests.sleeper_manager.cloudflare.test_live_hybrid_shadow import Sleeper, setup
from tests.sleeper_manager.workflows.planning_collection_support import NOW, _RecordingProjections


@pytest.mark.parametrize("shadow", [False, True])
def test_selected_hybrid_supplies_advice_with_independent_shadow_setting(
    shadow, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """One candidate batch supplies advice and, when enabled, both-roster pairs."""
    calls = []
    original = HybridRuntimeProjectionProvider.project_batch

    async def project(self, targets, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(targets)
        return await original(self, targets, **kwargs)

    monkeypatch.setattr(HybridRuntimeProjectionProvider, "project_batch", project)

    async def exercise() -> None:
        manager, opponent, database, store, policy = await setup()
        policy = replace(policy, active_projection_provider="hybrid", hybrid_shadow_enabled=shadow)

        async def collect(profile):  # type: ignore[no-untyped-def]
            assert shadow
            return opponent

        result = await finish_hybrid_shadow_planning(
            manager,
            sleeper=Sleeper(),
            collect_opponent=collect,
            control=_RecordingProjections(),
            policy=policy,
            store=store,
            archive=None,
            clock=lambda: NOW,
        )
        assert len(calls) == 1
        assert len(calls[0]) == (6 if shadow else 4)
        snapshots = [row.snapshot for row in result.inputs.projections if row.snapshot is not None]
        assert len(snapshots) == 4
        assert all(row.model_version.startswith("opening-week-hybrid-v6-") for row in snapshots)
        count = database.connection.execute("SELECT count(*) FROM hybrid_shadow_runs").fetchone()[0]
        assert count == int(shadow)

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["missing_store", "missing_history", "exception"])
def test_selected_hybrid_failure_never_substitutes_baseline_advice(failure, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A valid control forecast cannot hide a selected candidate's integrity gap."""

    async def fail(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("private diagnostic")

    if failure == "exception":
        monkeypatch.setattr(HybridRuntimeProjectionProvider, "project_batch", fail)

    async def exercise() -> None:
        manager, opponent, database, store, policy = await setup()
        policy = replace(policy, active_projection_provider="hybrid")
        if failure == "missing_history":
            policy = replace(policy, hybrid_history_version="missing")

        async def collect(profile):  # type: ignore[no-untyped-def]
            return opponent

        result = await finish_hybrid_shadow_planning(
            manager,
            sleeper=Sleeper(),
            collect_opponent=collect,
            control=_RecordingProjections(),
            policy=policy,
            store=None if failure == "missing_store" else store,
            archive=None,
            clock=lambda: NOW,
        )
        assert len(result.inputs.projections) == 4
        assert all(
            row.snapshot is None and row.missing_reason is not None
            for row in result.inputs.projections
        )
        if failure != "missing_store":
            row = database.connection.execute("SELECT * FROM hybrid_shadow_runs").fetchone()
            manifest = await store.load_value(row["manifest_artifact_id"])
            pair = await store.load_value(manifest["pairs"][0])
            assert pair["control"]["snapshot"] is not None
            assert pair["candidate"]["failure"] is not None

    asyncio.run(exercise())


def test_selected_hybrid_can_collect_without_baseline_observations(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """An unavailable diagnostic control must not block the selected provider's collection."""
    from types import SimpleNamespace

    from sleeper_manager.cloudflare.planning import collect_cloudflare_planning_inputs
    from sleeper_manager.domain.runtime_policy import default_runtime_policy
    from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
    from tests.sleeper_manager.cloudflare.test_cloudflare_planning import NOW as COLLECT_NOW
    from tests.sleeper_manager.cloudflare.test_cloudflare_planning import FixtureFetch

    calls = []

    async def finish(manager, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(manager)
        assert manager.inputs.projections == ()
        assert manager.inputs.projection_history_version == "pinned-hybrid"
        return manager

    monkeypatch.setattr("sleeper_manager.cloudflare.planning.finish_hybrid_shadow_planning", finish)

    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        await collect_cloudflare_planning_inputs(
            SimpleNamespace(SLEEPER_LEAGUE_ID="league-current", SLEEPER_USER_ID="user-manager"),
            FixtureFetch(),
            repository=repository,
            policy=replace(
                default_runtime_policy(history_version="empty-control"),
                active_projection_provider="hybrid",
                hybrid_history_version="pinned-hybrid",
            ),
            scheduled_at=COLLECT_NOW,
            clock=lambda: COLLECT_NOW,
        )
        assert len(calls) == 1
        assert await repository.load_projection_observations("empty-control") == ()

    asyncio.run(exercise())


def test_runtime_provider_selection_roundtrips_without_enabling_shadow() -> None:
    """Provider selection and evaluation remain independent policy fields."""

    async def exercise() -> None:
        _, _, _, _, policy = await setup()
        selected = replace(policy, active_projection_provider="hybrid", hybrid_shadow_enabled=False)
        assert RuntimePolicy.from_json(selected.version, selected.to_json()) == selected

    asyncio.run(exercise())
