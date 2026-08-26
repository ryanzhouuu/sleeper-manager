import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sleeper_manager.cli import build_parser
from sleeper_manager.cloudflare.runtime_sync import (
    d1_database_id,
    records_from_observations,
    redact_secrets,
    runtime_policy_for_history,
    sync_cloudflare_runtime_data,
)
from sleeper_manager.domain.scoring import BoxScoreLine
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.projections.direct_baseline import DirectBaselineObservation

NOW = datetime(2026, 1, 7, 18, tzinfo=UTC)


def _observation() -> DirectBaselineObservation:
    return DirectBaselineObservation(
        player_id="401",
        game_id="game-1",
        game_start=NOW - timedelta(days=2),
        outcome_finalized_at=NOW - timedelta(days=1),
        minutes=30,
        started=True,
        did_not_play=False,
        box_score=BoxScoreLine(points=20),
        source_version="espn-v1",
    )


def test_cli_exposes_sync_and_run_scheduled_commands() -> None:
    sync = build_parser().parse_args(["sync-cloudflare-runtime-data"])
    scheduled = build_parser().parse_args(["run-scheduled"])

    assert sync.command == "sync-cloudflare-runtime-data"
    assert sync.apply is False
    assert scheduled.command == "run-scheduled"


def test_sync_dry_run_does_not_write(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        history = records_from_observations((_observation(),), history_version="history-v1")
        policy = runtime_policy_for_history(history)
        report = await sync_cloudflare_runtime_data(
            history,
            policy=policy,
            repository=repository,
            apply=False,
            now=NOW,
        )
        assert report.apply is False
        assert report.observation_count == 1
        assert report.content_hash == history.content_hash
        assert report.policy_activated is False
        assert await repository.load_projection_observations("history-v1") == ()
        assert await repository.load_runtime_policy() is None

    asyncio.run(exercise())


def test_sync_apply_verifies_count_hash_and_activates_policy_last(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        history = records_from_observations((_observation(),), history_version="history-v1")
        policy = runtime_policy_for_history(history)
        report = await sync_cloudflare_runtime_data(
            history,
            policy=policy,
            repository=repository,
            apply=True,
            now=NOW,
        )
        loaded = await repository.load_projection_observations("history-v1")
        stored_policy = await repository.load_runtime_policy()
        assert report.apply is True
        assert report.policy_activated is True
        assert report.verified_count == 1
        assert report.verified_hash == history.content_hash
        assert len(loaded) == 1
        assert stored_policy is not None
        assert stored_policy.version == policy.version
        assert '"projection_history_version":"history-v1"' in stored_policy.payload_json

    asyncio.run(exercise())


def test_redact_secrets_strips_tokens() -> None:
    assert redact_secrets("CLOUDFLARE_API_TOKEN=abc") == "[redacted]"
    assert redact_secrets("observation_count=12") == "observation_count=12"


def test_d1_database_id_reads_wrangler_config() -> None:
    assert d1_database_id(Path("wrangler.toml")) == "ceabcb2a-68f6-4781-8c50-98f37a0e6044"
