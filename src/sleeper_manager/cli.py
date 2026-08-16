import argparse
import asyncio
import sys
from collections import Counter
from dataclasses import fields
from datetime import UTC, date, datetime
from pathlib import Path

from sleeper_manager import __version__
from sleeper_manager.backtesting.experiment import (
    Phase4ValidationError,
    run_model_feature_validation,
    run_phase4_validation_experiment,
)
from sleeper_manager.backtesting.lock_in_experiment import (
    LockInExperimentError,
    run_lock_in_policy_validation,
)
from sleeper_manager.config import Settings
from sleeper_manager.domain.league import LeagueProfile
from sleeper_manager.integrations.nba.espn import ESPNAPIError, ESPNClient
from sleeper_manager.integrations.sleeper.client import SleeperAPIError, SleeperClient
from sleeper_manager.integrations.sleeper.sync import (
    LeagueBootstrapError,
    LeagueSynchronizationService,
)
from sleeper_manager.notifications.factory import build_notification_dispatcher
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.nba_cache import SQLiteNBADataCache
from sleeper_manager.persistence.sqlite import SQLiteStateRepository
from sleeper_manager.workflows.nba_diagnostics import collect_nba_diagnostics
from sleeper_manager.workflows.notification_loop import (
    NotificationLoop,
    default_placeholder_request,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sleeper fantasy basketball decision assistant")
    parser.add_argument("--version", action="version", version=__version__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("check-config", help="Report whether required configuration is present")
    subcommands.add_parser("bootstrap", help="Validate and summarize the configured Sleeper league")
    nba_data = subcommands.add_parser(
        "check-nba-data", help="Report NBA provider health and current-roster mapping coverage"
    )
    nba_data.add_argument("--date", dest="game_date", help="Scoreboard date in YYYY-MM-DD format")
    subcommands.add_parser(
        "phase3-test-notification",
        help="Send one idempotent placeholder notification for the Phase 3 operational test",
    )
    validation = subcommands.add_parser(
        "validate-model-features",
        help="Run the frozen four-season model feature validation experiment",
    )
    validation.add_argument(
        "--workspace",
        type=Path,
        default=Path(".local/model-validation"),
        help="Ignored local directory containing raw sources, injury cache, and reports",
    )
    validation.add_argument(
        "--league-fixture",
        type=Path,
        default=Path("tests/fixtures/sleeper/current_league.json"),
        help="Sleeper league payload providing the scoring_settings object",
    )
    phase4 = subcommands.add_parser(
        "validate-phase4",
        help="Run the frozen interpretable-opportunity-model validation-closure experiment",
    )
    phase4.add_argument(
        "--workspace",
        type=Path,
        default=Path(".local/model-validation"),
        help="Ignored local directory containing raw sources, injury cache, and reports",
    )
    phase4.add_argument(
        "--league-fixture",
        type=Path,
        default=Path("tests/fixtures/sleeper/current_league.json"),
        help="Sleeper league payload providing the scoring_settings object",
    )
    phase4.add_argument(
        "--mode",
        choices=("development", "locked_retrospective"),
        default="development",
        help=(
            "development freezes the manifest and runs development folds only; "
            "locked_retrospective refuses on a missing/mismatched manifest and writes the "
            "complete report"
        ),
    )
    lock_in = subcommands.add_parser(
        "validate-lock-in-policy",
        help=(
            "Replay cached historical rosters and validate counterfactual Lock-In policy decisions"
        ),
    )
    lock_in.add_argument(
        "--workspace",
        type=Path,
        default=Path(".local/model-validation"),
        help="Ignored local directory containing cached Sleeper inputs and reports",
    )
    lock_in.add_argument("--current-league-id", required=True)
    lock_in.add_argument("--historical-league-id", required=True)
    lock_in.add_argument("--stress-league-id", required=True)
    lock_in.add_argument(
        "--refresh-sleeper",
        action="store_true",
        help="Explicitly request provider refresh instead of using cached inputs",
    )
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


def _diagnostic_date(value: str | None) -> date:
    if value is None:
        return datetime.now(UTC).date()
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("--date must use YYYY-MM-DD format") from error


async def _check_nba_data(settings: Settings, game_date: date) -> int:
    if not settings.sleeper_configured:
        print(
            "Sleeper configuration is incomplete: set SLEEPER_LEAGUE_ID and SLEEPER_USER_ID",
            file=sys.stderr,
        )
        return 2
    if settings.state_backend != "sqlite":
        print("Phase 2 NBA diagnostics require STATE_BACKEND=sqlite", file=sys.stderr)
        return 2

    try:
        policy = settings.load_manager_policy()
        repository = SQLiteStateRepository(settings.sqlite_path)
        repository.initialize()
        SQLiteNBADataCache(settings.sqlite_path).initialize()
        async with SleeperClient() as sleeper, ESPNClient() as nba:
            report = await collect_nba_diagnostics(
                sleeper,
                nba,
                league_id=settings.sleeper_league_id,
                user_id=settings.sleeper_user_id,
                game_date=game_date,
                mapping_overrides=policy.players.mapping_overrides,
            )
    except (LeagueBootstrapError, SleeperAPIError, ESPNAPIError, OSError, ValueError) as error:
        print(f"NBA diagnostics failed: {error}", file=sys.stderr)
        return 2

    print(f"NBA provider: {report.provider}")
    for quality in report.quality_reports:
        print(
            f"{quality.resource}: {quality.state.value} "
            f"({quality.record_count} records, retrieved {quality.retrieved_at.isoformat()})"
        )
    print(
        f"Roster mappings: {len(report.mapping.resolved)}/{len(report.mapping.mappings)} resolved"
    )
    for mapping in report.mapping.unresolved:
        print(f"Unresolved {mapping.sleeper_id}: {mapping.reason}")
    for warning in report.mapping.warnings:
        print(f"Warning: {warning}")
    for failure in report.errors:
        print(f"Error: {failure}")
    return 0 if report.healthy else 1


async def _phase3_test_notification(settings: Settings) -> int:
    if not settings.notifications_configured:
        print("Notification configuration is incomplete", file=sys.stderr)
        return 2
    if not settings.acknowledgement_base_url:
        print("Set ACKNOWLEDGEMENT_BASE_URL before sending a Phase 3 notification", file=sys.stderr)
        return 2
    try:
        repository = AsyncSQLiteStateRepository(settings.sqlite_path)
        await repository.initialize()
        dispatcher = build_notification_dispatcher(settings)
        now = datetime.now(UTC)
        result = await NotificationLoop(
            repository,
            dispatcher,
            acknowledgement_base_url=settings.acknowledgement_base_url,
        ).run(
            default_placeholder_request(
                league_id=settings.sleeper_league_id or "phase3-local",
                now=now,
            )
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Phase 3 notification failed: {error}", file=sys.stderr)
        return 2
    print(f"Phase 3 notification: {result.status}")
    print(f"Recommendation: {result.recommendation.recommendation_id}")
    if result.delivery is not None:
        for attempt in result.delivery.attempts:
            state = "succeeded" if attempt.succeeded else "failed"
            print(f"Delivery {attempt.provider}: {state}")
    return 0 if result.status != "delivery_failed" else 1


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
    if args.command == "check-nba-data":
        settings = Settings()
        try:
            game_date = _diagnostic_date(args.game_date)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
        return asyncio.run(_check_nba_data(settings, game_date))
    if args.command == "phase3-test-notification":
        settings = Settings()
        return asyncio.run(_phase3_test_notification(settings))
    if args.command == "validate-model-features":
        try:
            output = run_model_feature_validation(
                args.workspace,
                league_fixture=args.league_fixture,
            )
        except (OSError, RuntimeError, ValueError) as error:
            print(f"Model feature validation failed: {error}", file=sys.stderr)
            return 2
        print(f"Dataset: {output.dataset_version}")
        print(
            "Selected cumulative features: "
            + (", ".join(output.selected_features) if output.selected_features else "none")
        )
        for candidate, recommendation in output.recommendations:
            print(f"{candidate}: {recommendation}")
        print(f"Frozen manifest: {output.frozen_manifest_path}")
        print(f"JSON report: {output.report_json_path}")
        print(f"Markdown report: {output.report_markdown_path}")
        return 0
    if args.command == "validate-phase4":
        try:
            phase4_output = run_phase4_validation_experiment(
                args.workspace,
                league_fixture=args.league_fixture,
                mode=args.mode,
            )
        except (Phase4ValidationError, OSError, ValueError) as error:
            print(f"Phase 4 validation experiment failed: {error}", file=sys.stderr)
            return 2
        print(f"Mode: {phase4_output.mode}")
        print(f"Dataset: {phase4_output.dataset_version}")
        print(f"Frozen manifest: {phase4_output.manifest_path}")
        if phase4_output.report_json_path is not None:
            print(f"JSON report: {phase4_output.report_json_path}")
            print(f"Markdown report: {phase4_output.report_markdown_path}")
            print(f"Selected baseline: {phase4_output.selected_model}")
        return 0
    if args.command == "validate-lock-in-policy":
        try:
            lock_output = run_lock_in_policy_validation(
                args.workspace,
                current_league_id=args.current_league_id,
                historical_league_id=args.historical_league_id,
                stress_league_id=args.stress_league_id,
                refresh_sleeper=args.refresh_sleeper,
            )
        except (LockInExperimentError, OSError, ValueError) as error:
            print(f"Lock-In policy validation failed: {error}", file=sys.stderr)
            return 2
        print(f"Status: {lock_output.status}")
        print(f"JSON report: {lock_output.report_json_path}")
        print(f"Markdown report: {lock_output.report_markdown_path}")
        return 0 if lock_output.status == "complete" else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
