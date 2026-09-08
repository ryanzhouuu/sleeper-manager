"""Parse and execute the historical team-week bundle module command."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import sleeper_manager.backtesting.replay.team_week_bundle as team_week_bundle
from sleeper_manager.backtesting.replay.league_archive import LeagueArchiveError
from sleeper_manager.integrations.sleeper.client import SleeperAPIError


def _parse_monday(value: str) -> date:
    """Parse an ISO date argument, rejecting non-ISO values for module command parsing."""

    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("--monday must use YYYY-MM-DD") from error


def _parser() -> argparse.ArgumentParser:
    """Build the intentionally narrow command parser for one historical team-week."""

    parser = argparse.ArgumentParser(description="Build one historical replay input bundle")
    parser.add_argument("--workspace", type=Path, default=Path(".local/model-validation"))
    parser.add_argument("--league-id", required=True)
    parser.add_argument("--roster-id", required=True, type=int)
    parser.add_argument("--week", required=True, type=int)
    parser.add_argument("--monday", required=True, type=_parse_monday)
    parser.add_argument(
        "--inactive-evidence",
        type=Path,
        help="Reviewed final-inactive JSON ledger with retained PDFs",
    )
    parser.add_argument(
        "--identity-evidence", type=Path, help="Hashed ESPN roster identity sources"
    )
    parser.add_argument(
        "--injury-team-evidence",
        type=Path,
        help="Hashed injury reports for approximate historical teams",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Build one source-backed team-week and print its reproducible artifact paths."""

    args = _parser().parse_args(argv)
    try:
        output = team_week_bundle.bootstrap_historical_team_week_bundle(
            args.workspace,
            team_week_bundle.HistoricalTeamWeekBundleRequest(
                args.league_id,
                args.roster_id,
                args.week,
                args.monday,
            ),
            inactive_evidence_path=args.inactive_evidence,
            identity_evidence_path=args.identity_evidence,
            injury_team_evidence_path=args.injury_team_evidence,
        )
    except (
        team_week_bundle.HistoricalTeamWeekBundleError,
        LeagueArchiveError,
        OSError,
        SleeperAPIError,
        ValueError,
    ) as error:
        print(f"Historical team-week bundle failed: {error}")
        return 2
    print(f"Archive acquired: {output.archive_acquired}")
    print(f"Manifest: {output.manifest.manifest_id}")
    print(f"Bundle: {output.bundle_root}")
    print(f"Team-week: {output.team_week_path}")
    print(f"Eligibility quality: {output.team_week.eligibility_quality.value}")
    return 0


__all__ = ("main",)
