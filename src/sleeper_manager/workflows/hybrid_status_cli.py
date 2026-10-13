"""Reconstruct status-conditioned census history from verified retained official reports."""

import argparse
import json
import sys
from pathlib import Path

from sleeper_manager.backtesting.artifacts import atomic_write_bytes, canonical_json_bytes
from sleeper_manager.backtesting.replay.final_roster_evidence import load_final_roster_evidence
from sleeper_manager.backtesting.replay.inputs.models import GameRosterCensus
from sleeper_manager.backtesting.replay.status_history_inputs import load_status_history_inputs
from sleeper_manager.backtesting.replay.status_report_archive import load_status_report_archive
from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.projections.hybrid_history_artifact import load_hybrid_history_artifact
from sleeper_manager.projections.hybrid_status_history import attach_hybrid_status_history


def parser() -> argparse.ArgumentParser:
    """Require local provenance inputs and a separate destination; no network requests."""
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--history", type=Path, required=True)
    command.add_argument("--raw-dir", type=Path, required=True)
    command.add_argument("--injury-archive", type=Path, required=True)
    command.add_argument("--roster-ledger", type=Path, action="append", required=True)
    command.add_argument("--identity-ledger", type=Path)
    command.add_argument("--sleeper-catalog", type=Path)
    command.add_argument("--output-dir", type=Path, required=True)
    command.add_argument("--cutoff-minutes", type=int, default=30)
    return command


def run(args: argparse.Namespace) -> dict[str, object]:
    """Never overwrite different history; write full gap/source detail only to local artifacts."""
    if args.cutoff_minutes <= 0:
        raise ValueError("Status cutoff must be positive")
    history = load_hybrid_history_artifact(args.history)
    seasons = tuple(
        sorted(
            {
                nba_season_start_year(row.game_start) + 1
                for row in history.observations + history.opportunities
            }
        )
    )
    inputs, proofs = load_status_history_inputs(
        args.raw_dir,
        seasons,
        identity_ledger=args.identity_ledger,
        sleeper_catalog=args.sleeper_catalog,
    )
    censuses: list[GameRosterCensus] = []
    for path in args.roster_ledger:
        evidence = load_final_roster_evidence(path, inputs)
        censuses.extend(evidence.censuses)
        proofs += evidence.source_fingerprints
    game_ids = {row.game_id for row in history.opportunities}
    games = tuple(game for game in inputs.games if game.provider_id in game_ids)
    reports, report_proofs = load_status_report_archive(
        args.injury_archive, games, cutoff_minutes=args.cutoff_minutes
    )
    result = attach_hybrid_status_history(
        history,
        games=games,
        players=inputs.provider_players,
        teams=inputs.teams,
        censuses=tuple(censuses),
        reports=reports,
        sources=proofs + report_proofs,
        cutoff_minutes=args.cutoff_minutes,
    )
    outputs = {
        args.output_dir / "history.json": canonical_json_bytes(result.history),
        args.output_dir / "coverage.json": canonical_json_bytes(result.coverage),
    }
    if args.history.resolve() in {path.resolve() for path in outputs}:
        raise ValueError("Output must be separate from source history")
    for path, encoded in outputs.items():
        if path.exists() and path.read_bytes() != encoded:
            raise ValueError("Different output already exists")
    for path, encoded in outputs.items():
        atomic_write_bytes(path, encoded)
    return {
        key: result.coverage[key]
        for key in (
            "dataset_version",
            "added_count",
            "status_tagged_count",
            "opportunity_count",
            "observation_count",
            "buckets",
            "gap_counts",
            "evidence_semantics",
        )
    }


def main() -> int:
    """Keep private source rows out of terminal error output."""
    try:
        result = run(parser().parse_args())
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        print(f"Status history build failed: {type(error).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
