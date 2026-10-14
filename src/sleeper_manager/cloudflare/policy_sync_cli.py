"""Synchronize manager policy without rebuilding history or deploying the Worker."""

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from sleeper_manager.backtesting.experiments.data import scoring_policy_from_league_fixture
from sleeper_manager.cloudflare.policy_sync import sync_runtime_policy
from sleeper_manager.cloudflare.runtime_sync import RemoteD1, d1_database_id
from sleeper_manager.config import load_manager_policy
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.d1 import D1StateRepository
from sleeper_manager.persistence.hybrid_runtime import D1HybridRuntimeRepository
from sleeper_manager.persistence.hybrid_sqlite import SQLiteHybridRuntimeRepository


def parser() -> argparse.ArgumentParser:
    """Dry-run verifies the selected existing artifacts; writes require --apply."""
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--policy", type=Path, default=Path(".local/policy.toml"))
    command.add_argument(
        "--league-fixture", type=Path, default=Path("tests/fixtures/sleeper/current_league.json")
    )
    command.add_argument("--wrangler-config", type=Path, default=Path("wrangler.toml"))
    command.add_argument("--local-db", type=Path)
    command.add_argument("--apply", action="store_true")
    return command


async def run(args: argparse.Namespace) -> dict[str, object]:
    """Never initialize a database during a policy-only read or print private source payloads."""
    manager = load_manager_policy(args.policy)
    scoring = scoring_policy_from_league_fixture(args.league_fixture)
    now = datetime.now(UTC)
    if args.local_db is not None:
        if not args.local_db.is_file():
            raise ValueError("Initialize runtime history before synchronizing policy")
        return await sync_runtime_policy(
            manager,
            state=AsyncSQLiteStateRepository(args.local_db),
            store=SQLiteHybridRuntimeRepository(args.local_db),
            scoring_policy=scoring,
            apply=args.apply,
            now=now,
        )
    async with httpx.AsyncClient(timeout=60) as client:
        database = RemoteD1(
            os.environ.get("CLOUDFLARE_ACCOUNT_ID", ""),
            d1_database_id(args.wrangler_config),
            os.environ.get("CLOUDFLARE_API_TOKEN", ""),
            client,
        )
        return await sync_runtime_policy(
            manager,
            state=D1StateRepository(database),
            store=D1HybridRuntimeRepository(database),
            scoring_policy=scoring,
            apply=args.apply,
            now=now,
        )


def main() -> int:
    """Expose sanitized readiness results; a failed verification never authorizes activation."""
    try:
        report = asyncio.run(run(parser().parse_args()))
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Runtime policy sync failed: {type(error).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
