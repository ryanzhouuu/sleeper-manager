"""Selected hybrid advice fails closed and never substitutes baseline projections."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.cloudflare.hybrid_planning import finish_hybrid_planning
from sleeper_manager.cloudflare.hybrid_sync import (
    build_hybrid_runtime_export,
    sync_hybrid_runtime_history,
)
from sleeper_manager.domain.runtime_policy import RuntimePolicy, default_runtime_policy
from sleeper_manager.persistence.hybrid_runtime import (
    HYBRID_RUNTIME_SCHEMA,
    D1HybridRuntimeRepository,
)
from sleeper_manager.projections.hybrid_runtime_provider import HybridRuntimeProjectionProvider
from sleeper_manager.projections.hybrid_types import HybridHistory
from tests.sleeper_manager.persistence.test_d1 import FakeD1
from tests.sleeper_manager.projections.hybrid_support import history
from tests.sleeper_manager.workflows.planning_collection_support import (
    NOW,
    _collect,
    _nba,
    _RecordingProjections,
)


def _shifted(data: HybridHistory) -> HybridHistory:
    """Move fixture history a season back so it precedes the live decision cutoff."""
    year = timedelta(days=365)
    return HybridHistory(
        "live-hybrid-fixture",
        tuple(
            replace(row, game_start=row.game_start - year, finalized_at=row.finalized_at - year)
            for row in data.observations
        ),
        tuple(
            replace(row, game_start=row.game_start - year, finalized_at=row.finalized_at - year)
            for row in data.opportunities
        ),
    )


async def setup():  # type: ignore[no-untyped-def]
    manager = await _collect(_nba(), _RecordingProjections(), skip_projections=True)
    profile = manager.inputs.league_profile
    export = build_hybrid_runtime_export(
        _shifted(history()),
        reference_time=NOW,
        game_start=manager.projection_targets[0].game_start,
        scoring_policy=profile.scoring,
    )
    database = FakeD1()
    await database.exec(HYBRID_RUNTIME_SCHEMA)
    store = D1HybridRuntimeRepository(database)
    await sync_hybrid_runtime_history(export, store=store, apply=True, now=NOW, clock=lambda: NOW)
    policy = replace(
        default_runtime_policy(history_version="control"),
        hybrid_history_version=export.manifest.version,
        active_projection_provider="hybrid",
    )
    return manager, store, policy


def test_selected_hybrid_projects_the_managers_targets_in_one_batch(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = []
    original = HybridRuntimeProjectionProvider.project_batch

    async def project(self, targets, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(targets)
        return await original(self, targets, **kwargs)

    monkeypatch.setattr(HybridRuntimeProjectionProvider, "project_batch", project)

    async def exercise() -> None:
        manager, store, policy = await setup()
        result = await finish_hybrid_planning(
            manager, policy=policy, store=store, archive=None, clock=lambda: NOW
        )
        assert [len(batch) for batch in calls] == [4]
        snapshots = [row.snapshot for row in result.inputs.projections if row.snapshot is not None]
        assert len(snapshots) == 4
        assert all(row.model_version.startswith("opening-week-hybrid-v6-") for row in snapshots)
        assert "hybrid_forecast_archive_missing" in result.warnings

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["missing_store", "missing_history", "exception", "timeout"])
def test_selected_hybrid_failure_leaves_explicit_gaps(failure, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def fail(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("private diagnostic")

    async def slow(*args, **kwargs):  # type: ignore[no-untyped-def]
        await asyncio.sleep(1)

    if failure == "exception":
        monkeypatch.setattr(HybridRuntimeProjectionProvider, "project_batch", fail)
    if failure == "timeout":
        monkeypatch.setattr(HybridRuntimeProjectionProvider, "project_batch", slow)
        monkeypatch.setattr(
            "sleeper_manager.cloudflare.hybrid_planning.HYBRID_TIMEOUT_SECONDS", 0.01
        )

    async def exercise() -> None:
        manager, store, policy = await setup()
        if failure == "missing_history":
            policy = replace(policy, hybrid_history_version="missing")
        result = await finish_hybrid_planning(
            manager,
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
        assert ("hybrid_store_missing" in result.warnings) == (failure == "missing_store")

    asyncio.run(exercise())


def test_advice_cutoff_advances_after_hybrid_computation() -> None:
    async def exercise() -> None:
        manager, store, policy = await setup()
        times = iter((NOW, NOW + timedelta(minutes=1)))
        result = await finish_hybrid_planning(
            manager, policy=policy, store=store, archive=None, clock=lambda: next(times)
        )
        assert result.decision_time == NOW + timedelta(minutes=1)
        assert all(row.snapshot.available_as_of == NOW for row in result.inputs.projections)

    asyncio.run(exercise())


def test_selected_hybrid_can_collect_without_baseline_observations(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    from sleeper_manager.cloudflare.planning import collect_cloudflare_planning_inputs
    from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
    from tests.sleeper_manager.cloudflare.test_cloudflare_planning import NOW as COLLECT_NOW
    from tests.sleeper_manager.cloudflare.test_cloudflare_planning import FixtureFetch

    calls = []

    async def finish(manager, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(manager)
        assert manager.inputs.projections == ()
        assert manager.inputs.projection_history_version == "pinned-hybrid"
        return manager

    monkeypatch.setattr("sleeper_manager.cloudflare.planning.finish_hybrid_planning", finish)

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

    asyncio.run(exercise())


def test_runtime_provider_selection_roundtrips() -> None:
    async def exercise() -> None:
        _, _, policy = await setup()
        assert RuntimePolicy.from_json(policy.version, policy.to_json()) == policy

    asyncio.run(exercise())
