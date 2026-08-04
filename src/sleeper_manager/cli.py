import argparse
import asyncio
import sys
from collections import Counter
from dataclasses import fields

from sleeper_manager import __version__
from sleeper_manager.config import Settings
from sleeper_manager.domain.league import LeagueProfile
from sleeper_manager.integrations.sleeper.client import SleeperAPIError, SleeperClient
from sleeper_manager.integrations.sleeper.sync import (
    LeagueBootstrapError,
    LeagueSynchronizationService,
)
from sleeper_manager.persistence.sqlite import SQLiteStateRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sleeper fantasy basketball decision assistant")
    parser.add_argument("--version", action="version", version=__version__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("check-config", help="Report whether required configuration is present")
    subcommands.add_parser("bootstrap", help="Validate and summarize the configured Sleeper league")
    return parser


def _format_slots(profile: LeagueProfile) -> str:
    counts = Counter(slot.position for slot in profile.roster_slots if slot.is_starting)
    return ", ".join(f"{position} x{counts[position]}" for position in counts)


def _format_scoring(profile: LeagueProfile) -> str:
    values = []
    for field in fields(profile.scoring):
        value = getattr(profile.scoring, field.name)
        if value:
            values.append(f"{field.name}={value:g}")
    return ", ".join(values) if values else "all zero"


async def _bootstrap(settings: Settings) -> int:
    if not settings.sleeper_configured:
        print(
            "Sleeper configuration is incomplete: set SLEEPER_LEAGUE_ID and SLEEPER_USER_ID",
            file=sys.stderr,
        )
        return 2
    if settings.state_backend != "sqlite":
        print("Phase 1 bootstrap requires STATE_BACKEND=sqlite", file=sys.stderr)
        return 2

    try:
        policy = settings.load_manager_policy()
        repository = SQLiteStateRepository(settings.sqlite_path)
        repository.initialize()
        async with SleeperClient() as sleeper:
            result = await LeagueSynchronizationService(
                sleeper,
                profile_store=repository,
            ).sync(
                league_id=settings.sleeper_league_id,
                user_id=settings.sleeper_user_id,
            )
    except (LeagueBootstrapError, SleeperAPIError, OSError, ValueError) as error:
        print(f"League bootstrap failed: {error}", file=sys.stderr)
        return 2

    profile = result.profile
    manager_roster = next(
        roster for roster in profile.rosters if roster.roster_id == profile.manager_roster_id
    )
    print(f"League: {profile.name} ({profile.league_id})")
    print(f"Mode: {profile.mode.value}")
    print(f"Season: {profile.season} {profile.season_type}")
    print(f"Current fantasy week: {profile.fantasy_week.week}")
    print(f"Manager roster: {profile.manager_roster_id}")
    print(f"Rostered players: {len(manager_roster.player_ids)}")
    print(f"Starting slots: {_format_slots(profile)}")
    print(f"Scoring: {_format_scoring(profile)}")
    print(f"Manager policy: {policy.decision.preset} ({policy.version})")
    print(f"Configuration fingerprint: {profile.configuration_fingerprint}")
    print(f"Configuration changed: {result.configuration_changed}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check-config":
        settings = Settings()
        print(f"Sleeper configured: {settings.sleeper_configured}")
        print(f"Notifications configured: {settings.notifications_configured}")
        print(f"State backend: {settings.state_backend}")
        print(f"Timezone: {settings.timezone}")
        return 0
    if args.command == "bootstrap":
        settings = Settings()
        return asyncio.run(_bootstrap(settings))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
