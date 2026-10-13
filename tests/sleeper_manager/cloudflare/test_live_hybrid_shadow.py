"""Live shadow retains both rosters' diagnostics while control advice survives failures."""

import asyncio
from dataclasses import replace
from datetime import timedelta

from sleeper_manager.cloudflare.hybrid_sync import (
    build_hybrid_runtime_export,
    sync_hybrid_runtime_history,
)
from sleeper_manager.cloudflare.shadow_planning import finish_hybrid_shadow_planning
from sleeper_manager.domain.models import Roster
from sleeper_manager.domain.runtime_policy import default_runtime_policy
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


class Sleeper:
    async def matchups(self, league, week):  # type: ignore[no-untyped-def]
        return [
            {"roster_id": 1, "matchup_id": 1, "players": ["p1", "p2"]},
            {"roster_id": 2, "matchup_id": 1, "players": ["p3"]},
        ]


async def setup():  # type: ignore[no-untyped-def]
    manager = await _collect(_nba(), _RecordingProjections(), skip_projections=True)
    roster = Roster(2, None, ("p3",), ("p3",))
    profile = replace(
        manager.inputs.league_profile,
        total_rosters=2,
        rosters=(*manager.inputs.league_profile.rosters, roster),
    )
    manager = replace(manager, inputs=replace(manager.inputs, league_profile=profile))
    opponent = replace(
        manager,
        inputs=replace(manager.inputs, league_profile=replace(profile, manager_roster_id=2)),
        projection_targets=tuple(
            replace(target, sleeper_player_id="p3", provider_player_id="402")
            for target in manager.projection_targets[:2]
        ),
    )
    data = history()
    data = HybridHistory(
        "live-shadow-fixture",
        tuple(
            replace(
                row,
                game_start=row.game_start - timedelta(days=365),
                finalized_at=row.finalized_at - timedelta(days=365),
            )
            for row in data.observations
        ),
        tuple(
            replace(
                row,
                game_start=row.game_start - timedelta(days=365),
                finalized_at=row.finalized_at - timedelta(days=365),
            )
            for row in data.opportunities
        ),
    )
    export = build_hybrid_runtime_export(
        data,
        reference_time=NOW,
        game_start=manager.projection_targets[0].game_start,
        scoring_policy=profile.scoring,
    )
    database = FakeD1()
    await database.exec(HYBRID_RUNTIME_SCHEMA)
    store = D1HybridRuntimeRepository(database)
    await sync_hybrid_runtime_history(
        export, store=store, state=None, apply=True, enable_shadow=False, now=NOW, clock=lambda: NOW
    )
    policy = replace(
        default_runtime_policy(history_version="control"),
        hybrid_history_version=export.manifest.version,
        hybrid_shadow_enabled=True,
    )
    return manager, opponent, database, store, policy


def test_live_shadow_pairs_both_rosters_and_never_replaces_active_projections() -> None:
    async def exercise() -> None:
        manager, opponent, database, store, policy = await setup()

        async def collect(profile):  # type: ignore[no-untyped-def]
            assert profile.manager_roster_id == 2
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
        assert len(result.inputs.projections) == 4
        assert all(row.snapshot.model_version == "baseline-v1" for row in result.inputs.projections)
        row = database.connection.execute("SELECT * FROM hybrid_shadow_runs").fetchone()
        manifest = await store.load_value(row["manifest_artifact_id"])
        assert len(manifest["pairs"]) == 6
        assert len(manifest["rosters"]) == 2
        assert manifest["timing"] == "on_time"
        pair = await store.load_value(manifest["pairs"][0])
        assert pair["candidate"]["projection"]["model_version"].startswith(
            "opening-week-hybrid-v6-"
        )
        assert pair["candidate"]["participation"]["fallback"] == "missing_game_availability"
        assert await store.load_value(pair["candidate"]["joint_weights_artifact_id"])
        assert pair["control"]["snapshot"]["model_version"] == "baseline-v1"

    asyncio.run(exercise())


def test_candidate_history_failure_is_recorded_and_control_advice_still_exists() -> None:
    async def exercise() -> None:
        manager, _, database, store, policy = await setup()
        policy = replace(policy, hybrid_history_version="missing")

        async def collect(profile):  # type: ignore[no-untyped-def]
            raise RuntimeError("private payload should not enter telemetry")

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
        assert len(result.inputs.projections) == 4
        row = database.connection.execute("SELECT * FROM hybrid_shadow_runs").fetchone()
        manifest = await store.load_value(row["manifest_artifact_id"])
        assert any("opponent_gap" in warning for warning in manifest["warnings"])
        for artifact in manifest["pairs"]:
            pair = await store.load_value(artifact)
            assert pair["candidate"]["failure"] == "hybrid_history_version_missing"
            assert pair["expected_value_delta"] is None

    asyncio.run(exercise())


def test_shadow_persistence_failure_is_visible_and_does_not_suppress_control(capsys) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        manager, opponent, database, _, policy = await setup()

        class BrokenStore(D1HybridRuntimeRepository):
            async def save_shadow_run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                raise OSError("private payload")

        async def collect(profile):  # type: ignore[no-untyped-def]
            return opponent

        result = await finish_hybrid_shadow_planning(
            manager,
            sleeper=Sleeper(),
            collect_opponent=collect,
            control=_RecordingProjections(),
            policy=policy,
            store=BrokenStore(database),
            archive=None,
            clock=lambda: NOW,
        )
        assert len(result.inputs.projections) == 4
        assert "hybrid_shadow_persistence_failed:OSError" in result.warnings

    asyncio.run(exercise())
    output = capsys.readouterr().err
    assert '"outcome": "failed"' in output
    assert "private payload" not in output


def test_candidate_timeout_is_a_persisted_blocker_not_a_control_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def slow(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        await asyncio.sleep(1)

    monkeypatch.setattr(HybridRuntimeProjectionProvider, "project_batch", slow)
    monkeypatch.setattr("sleeper_manager.cloudflare.shadow_planning.SHADOW_TIMEOUT_SECONDS", 0.01)

    async def exercise() -> None:
        manager, opponent, database, store, policy = await setup()

        async def collect(profile):  # type: ignore[no-untyped-def]
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
        assert len(result.inputs.projections) == 4
        row = database.connection.execute("SELECT * FROM hybrid_shadow_runs").fetchone()
        manifest = await store.load_value(row["manifest_artifact_id"])
        pair = await store.load_value(manifest["pairs"][0])
        assert pair["candidate"]["failure"] == "candidate_exception:TimeoutError"

    asyncio.run(exercise())


def test_advice_cutoff_advances_after_shadow_computation() -> None:
    async def exercise() -> None:
        manager, opponent, _, store, policy = await setup()
        times = iter((NOW, NOW + timedelta(minutes=1), NOW + timedelta(minutes=2)))

        async def collect(profile):  # type: ignore[no-untyped-def]
            return opponent

        result = await finish_hybrid_shadow_planning(
            manager,
            sleeper=Sleeper(),
            collect_opponent=collect,
            control=_RecordingProjections(),
            policy=policy,
            store=store,
            archive=None,
            clock=lambda: next(times),
        )
        assert result.decision_time == NOW + timedelta(minutes=2)
        assert all(row.snapshot.available_as_of == NOW for row in result.inputs.projections)

    asyncio.run(exercise())
