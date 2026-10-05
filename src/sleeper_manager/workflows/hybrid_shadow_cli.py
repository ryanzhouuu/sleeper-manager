"""Local executable for paired opening-week candidate/control sidecars."""

import argparse
from pathlib import Path

from sleeper_manager.backtesting.artifacts import atomic_write_json
from sleeper_manager.backtesting.experiments.data import scoring_policy_from_league_fixture
from sleeper_manager.backtesting.replay.hybrid_projection_surface import (
    build_hybrid_projection_surfaces,
)
from sleeper_manager.backtesting.replay.inputs.artifact import load_historical_team_week_artifact
from sleeper_manager.backtesting.replay.projection_surface_artifact import (
    load_historical_projection_surface_artifact,
    write_historical_projection_surface_artifact,
)
from sleeper_manager.persistence.forecast_sqlite import SQLiteForecastArchiveRepository
from sleeper_manager.projections.hybrid_config import HybridProjectionConfig
from sleeper_manager.projections.hybrid_history_artifact import (
    load_hybrid_availability_artifact,
    load_hybrid_history_artifact,
)
from sleeper_manager.projections.hybrid_provider import HybridProjectionProvider
from sleeper_manager.workflows.hybrid_shadow import compare_hybrid_shadow


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", required=True, type=Path)
    parser.add_argument(
        "--league", required=True, type=Path, help="League JSON with scoring_settings"
    )
    parser.add_argument(
        "--team-week", required=True, type=Path, nargs=2, metavar=("OWN", "OPPONENT")
    )
    parser.add_argument(
        "--control-surface", required=True, type=Path, nargs=2, metavar=("OWN", "OPPONENT")
    )
    parser.add_argument("--forecast-archive", type=Path)
    parser.add_argument(
        "--availability", type=Path, help="Cutoff/player/game report assignments JSON"
    )
    parser.add_argument("--internal-only", action="store_true")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    weeks = tuple(load_historical_team_week_artifact(path) for path in args.team_week)
    controls = tuple(
        load_historical_projection_surface_artifact(path, team_week=week)
        for path, week in zip(args.control_surface, weeks, strict=True)
    )
    archive = None
    if args.forecast_archive is not None and not args.internal_only:
        if not args.forecast_archive.is_file():
            parser.error("--forecast-archive must name an existing initialized SQLite archive")
        archive = SQLiteForecastArchiveRepository(args.forecast_archive)
    provider = HybridProjectionProvider(
        load_hybrid_history_artifact(args.history),
        archive=archive,
        config=HybridProjectionConfig(use_external=not args.internal_only),
    )
    built = build_hybrid_projection_surfaces(
        weeks,
        provider,
        scoring_policy=scoring_policy_from_league_fixture(args.league),
        availability=load_hybrid_availability_artifact(args.availability)
        if args.availability is not None
        else None,
    )
    comparison = compare_hybrid_shadow(weeks, built, controls)
    for surface in built.surfaces:
        write_historical_projection_surface_artifact(
            args.output_dir / f"roster-{surface.roster_id}-candidate.json", surface
        )
    atomic_write_json(
        args.output_dir / "shadow.json",
        {"fingerprint": comparison.fingerprint, "comparison": comparison},
    )
    blocked = sum(pair.candidate.failure is not None for pair in comparison.pairs)
    print(
        f"{provider.model_version}: {len(comparison.pairs)} paired opportunities, "
        f"{blocked} blocked; {args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
