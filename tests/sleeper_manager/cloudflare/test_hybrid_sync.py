"""Offline export and shadow activation preserve complete evidence and activate policy last."""

import asyncio
from datetime import timedelta

import pytest

from sleeper_manager.cloudflare.hybrid_sync import (
    build_hybrid_runtime_export,
    sync_hybrid_runtime_history,
)
from sleeper_manager.cloudflare.hybrid_sync_cli import parser
from sleeper_manager.domain.runtime_policy import RuntimePolicy, default_runtime_policy
from sleeper_manager.persistence.base import RuntimePolicyRecord
from sleeper_manager.persistence.d1 import D1StateRepository
from sleeper_manager.persistence.hybrid_runtime import (
    HYBRID_RUNTIME_SCHEMA,
    D1HybridRuntimeRepository,
)
from sleeper_manager.persistence.statements import D1_SCHEMA
from sleeper_manager.projections.hybrid_config import HybridProjectionError
from tests.sleeper_manager.persistence.test_d1 import FakeD1
from tests.sleeper_manager.projections.hybrid_support import CUTOFF, TIPOFF, history
from tests.sleeper_manager.projections.test_hybrid_runtime_index import POLICY


def exported():  # type: ignore[no-untyped-def]
    return build_hybrid_runtime_export(
        history(), reference_time=CUTOFF, game_start=TIPOFF, scoring_policy=POLICY
    )


def test_export_is_complete_deterministic_and_dry_run_writes_nothing() -> None:
    export = exported()
    assert export == exported()
    assert len(export.manifest.player_artifacts) == 7
    assert export.manifest.census_artifacts
    report = asyncio.run(
        sync_hybrid_runtime_history(
            export, store=None, state=None, apply=False, enable_shadow=True, now=CUTOFF
        )
    )
    assert report["observation_count"] == 210
    assert report["opportunity_count"] == 210
    assert not report["shadow_enabled"]
    assert report["active_projection_provider"] == "direct_baseline"


def test_apply_verifies_history_before_enabling_shadow_and_preserves_manager_policy() -> None:
    async def exercise() -> None:
        database = FakeD1()
        await database.exec(D1_SCHEMA + HYBRID_RUNTIME_SCHEMA)
        state = D1StateRepository(database)
        initial = default_runtime_policy(history_version="control-history")
        await state.save_runtime_policy(
            RuntimePolicyRecord(initial.version, initial.to_json(), CUTOFF)
        )
        export = exported()
        report = await sync_hybrid_runtime_history(
            export,
            store=D1HybridRuntimeRepository(database),
            state=state,
            apply=True,
            enable_shadow=True,
            now=CUTOFF,
            clock=lambda: CUTOFF + timedelta(seconds=1),
        )
        record = await state.load_runtime_policy()
        policy = RuntimePolicy.from_json(record.version, record.payload_json)
        assert policy.hybrid_history_version == export.manifest.version
        assert policy.hybrid_shadow_enabled
        assert policy.manager_intent == initial.manager_intent
        assert policy.projection_history_version == initial.projection_history_version
        assert policy.active_projection_provider == "direct_baseline"
        assert report["shadow_enabled"]
        assert (
            database.connection.execute("SELECT count(*) FROM hybrid_history_versions").fetchone()[
                0
            ]
            == 1
        )

    asyncio.run(exercise())


def test_shadow_activation_requires_an_existing_advisor_policy() -> None:
    with pytest.raises(HybridProjectionError, match="existing_runtime_policy"):
        asyncio.run(
            sync_hybrid_runtime_history(
                exported(), store=object(), state=None, apply=True, enable_shadow=True, now=CUTOFF
            )
        )


def test_cli_keeps_apply_and_activation_explicit() -> None:
    args = parser().parse_args(
        ["--history", "history.json", "--league-fixture", "league.json", "--season", "2026"]
    )
    assert not args.apply
    assert not args.enable_shadow
    assert args.local_db is None
