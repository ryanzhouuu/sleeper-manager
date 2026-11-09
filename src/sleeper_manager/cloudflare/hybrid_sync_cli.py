"""Explicit operator command for indexed history synchronization; never promote hybrid advice."""

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from sleeper_manager.cloudflare.hybrid_sync import (
    build_hybrid_runtime_export,
    sync_hybrid_runtime_history,
)
from sleeper_manager.cloudflare.runtime_sync import RemoteD1, d1_database_id
from sleeper_manager.persistence.hybrid_runtime import D1HybridRuntimeRepository
from sleeper_manager.persistence.hybrid_sqlite import SQLiteHybridRuntimeRepository
from sleeper_manager.projections.hybrid_history_artifact import load_hybrid_history_artifact


def parser() -> argparse.ArgumentParser:
    """Keep remote writes explicit; policy activation stays in the policy sync command."""
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--history", type=Path, required=True)
    command.add_argument("--league-fixture", type=Path, required=True)
    command.add_argument("--season", type=int, required=True)
    command.add_argument("--apply", action="store_true")
    command.add_argument("--local-db", type=Path)
    command.add_argument("--wrangler-config", type=Path, default=Path("wrangler.toml"))
    return command


async def run(args: argparse.Namespace) -> dict[str, object]:
    """Read private source files locally and print only identities, counts, and readiness."""
    from sleeper_manager.backtesting.experiments.data import scoring_policy_from_league_fixture

    now = datetime.now(UTC)
    history = load_hybrid_history_artifact(args.history)
    export = build_hybrid_runtime_export(
        history,
        reference_time=now,
        game_start=datetime(args.season, 10, 1, tzinfo=UTC),
        scoring_policy=scoring_policy_from_league_fixture(args.league_fixture),
    )
    if not args.apply:
        return await sync_hybrid_runtime_history(export, store=None, apply=False, now=now)
    if args.local_db is not None:
        store = SQLiteHybridRuntimeRepository(args.local_db)
        await store.initialize()
        return await sync_hybrid_runtime_history(export, store=store, apply=True, now=now)
    async with httpx.AsyncClient(timeout=60) as client:
        database = RemoteD1(
            os.environ.get("CLOUDFLARE_ACCOUNT_ID", ""),
            d1_database_id(args.wrangler_config),
            os.environ.get("CLOUDFLARE_API_TOKEN", ""),
            client,
        )
        return await sync_hybrid_runtime_history(
            export,
            store=D1HybridRuntimeRepository(database),
            apply=True,
            now=now,
        )


def main() -> int:
    """Report sanitized failures; source payloads and credentials never enter command output."""
    try:
        report = asyncio.run(run(parser().parse_args()))
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Hybrid runtime sync failed: {type(error).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
