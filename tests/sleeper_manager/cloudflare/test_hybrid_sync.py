"""Offline export publishes complete evidence without touching the runtime policy."""

import asyncio
from datetime import timedelta

import pytest

from sleeper_manager.cloudflare.hybrid_sync import (
    build_hybrid_runtime_export,
    sync_hybrid_runtime_history,
)
from sleeper_manager.cloudflare.hybrid_sync_cli import parser
from sleeper_manager.domain.runtime_policy import default_runtime_policy
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
    report = asyncio.run(sync_hybrid_runtime_history(export, store=None, apply=False, now=CUTOFF))
    assert report["observation_count"] == 210
    assert report["opportunity_count"] == 210
    assert "shadow_enabled" not in report


def test_apply_publishes_verified_history_and_leaves_policy_unchanged() -> None:
    async def exercise() -> None:
        database = FakeD1()
        await database.exec(D1_SCHEMA + HYBRID_RUNTIME_SCHEMA)
        state = D1StateRepository(database)
        initial = default_runtime_policy(history_version="control-history")
        stored = RuntimePolicyRecord(initial.version, initial.to_json(), CUTOFF)
        await state.save_runtime_policy(stored)
        export = exported()
        await sync_hybrid_runtime_history(
            export,
            store=D1HybridRuntimeRepository(database),
            apply=True,
            now=CUTOFF,
            clock=lambda: CUTOFF + timedelta(seconds=1),
        )
        assert await state.load_runtime_policy() == stored
        assert (
            database.connection.execute("SELECT count(*) FROM hybrid_history_versions").fetchone()[
                0
            ]
            == 1
        )

    asyncio.run(exercise())


def test_apply_requires_a_store() -> None:
    with pytest.raises(HybridProjectionError, match="store_missing"):
        asyncio.run(sync_hybrid_runtime_history(exported(), store=None, apply=True, now=CUTOFF))


def test_cli_keeps_apply_explicit_and_has_no_shadow_flag() -> None:
    args = parser().parse_args(
        ["--history", "history.json", "--league-fixture", "league.json", "--season", "2026"]
    )
    assert not args.apply
    assert not hasattr(args, "enable_shadow")
    assert args.local_db is None
