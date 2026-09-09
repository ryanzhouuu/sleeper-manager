"""Account for explicitly selected input bundles without inferring replay readiness.

Missing selections remain in the denominator. No directory scan chooses source versions.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from sleeper_manager.backtesting.artifacts import (
    atomic_write_bytes,
    canonical_json_bytes,
    sha256_bytes,
)
from sleeper_manager.backtesting.experiments.input_index_models import (
    ExperimentInputIndex,
    FileReference,
    InputSelection,
    LeagueSample,
    TeamWeekKey,
)
from sleeper_manager.backtesting.replay.inputs.artifact import load_historical_team_week_artifact
from sleeper_manager.backtesting.replay.inputs.models import ReplayInputManifest


class ExperimentInputIndexError(ValueError):
    """A selected source is corrupt, incompatible, ambiguous or outside the sample."""


def file_reference(path: Path) -> FileReference:
    """Capture an absolute reference; subsequent inventory reads verify its bytes again."""
    return FileReference(path=str(path.resolve()), sha256=sha256_bytes(path.read_bytes()))


def _verified_path(reference: FileReference, base: Path) -> Path:
    path = (base / reference.path).resolve()
    if sha256_bytes(path.read_bytes()) != reference.sha256:
        raise ExperimentInputIndexError(f"Referenced file hash mismatch: {path}")
    return path


def _resolved_index(index: ExperimentInputIndex, base: Path) -> ExperimentInputIndex:
    """Keep copied indexes replayable when the authoring file used relative references."""

    def resolve(value: Any) -> Any:
        if isinstance(value, dict):
            if set(value) == {"path", "sha256"}:
                return {**value, "path": str((base / value["path"]).resolve())}
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return value

    return ExperimentInputIndex.model_validate(resolve(index.model_dump(mode="json")))


def build_input_inventory(index: ExperimentInputIndex, *, base: Path) -> dict[str, Any]:
    """Validate all references and emit every expected key, including failures and missing work."""
    index = _resolved_index(index, base)
    _verified_path(index.protocol, base)
    if index.policy_configuration is not None:
        _verified_path(index.policy_configuration, base)
    expected: dict[TeamWeekKey, LeagueSample] = {}
    for league in index.leagues:
        for reference in league.evidence:
            _verified_path(reference, base)
        for week in league.weeks:
            for roster_id in league.roster_ids:
                expected[
                    TeamWeekKey(
                        league_id=league.league_id,
                        season=league.season,
                        week=week.week,
                        roster_id=roster_id,
                    )
                ] = league
    selected: dict[TeamWeekKey, InputSelection] = {}
    for selection in index.selections:
        if selection.key not in expected:
            raise ExperimentInputIndexError("Selected team-week is outside the declared sample")
        previous = selected.get(selection.key)
        if previous is not None and previous != selection:
            raise ExperimentInputIndexError("Conflicting selections require an explicit choice")
        selected[selection.key] = selection
    rows = []
    for key in sorted(expected, key=lambda k: (k.league_id, k.season, k.week, k.roster_id)):
        chosen = selected.get(key)
        row: dict[str, Any] = {
            **key.model_dump(),
            "status": "unprocessed",
            "strict_complete": False,
        }
        if chosen is not None:
            for reference in chosen.outputs:
                _verified_path(reference, base)
            row["selection"] = chosen.model_dump(mode="json")
            if chosen.failure is not None:
                _verified_path(chosen.failure.evidence, base)
                row.update(status="failed_before_assembly", failure=chosen.failure.model_dump())
            else:
                row.update(_bundle_inventory(index, expected[key], chosen, base))
        rows.append(row)
    summaries = []
    for league in index.leagues:
        subset = [
            r for r in rows if (r["league_id"], r["season"]) == (league.league_id, league.season)
        ]
        summaries.append(
            {
                "league_id": league.league_id,
                "season": league.season,
                "role": league.role,
                "expected_team_weeks": len(subset),
                "status_counts": dict(Counter(r["status"] for r in subset)),
                "strict_complete_team_weeks": sum(r["strict_complete"] for r in subset),
                "unbound_calendar_weeks": [
                    w.week for w in league.weeks if w.boundaries_fingerprint is None
                ],
            }
        )
    return {
        "schema_version": "experiment-input-inventory-v1",
        "index_id": sha256_bytes(canonical_json_bytes(index.model_dump(mode="json"))),
        "replay_readiness": "not_evaluated",
        "expected_team_weeks": len(rows),
        "unresolved_bindings": list(index.unresolved_bindings),
        "leagues": summaries,
        "team_weeks": rows,
    }


def _bundle_inventory(
    index: ExperimentInputIndex, league: LeagueSample, selection: InputSelection, base: Path
) -> dict[str, Any]:
    """Require logical, content and declared configuration agreement before counting a bundle."""
    assert selection.bundle is not None
    manifest_path = _verified_path(selection.bundle.manifest, base)
    team_week_path = _verified_path(selection.bundle.team_week, base)
    payload = json.loads(manifest_path.read_bytes())
    if not isinstance(payload, dict):
        raise ExperimentInputIndexError("Manifest must be an object")
    manifest_id = payload.pop("manifest_id", None)
    manifest = TypeAdapter(ReplayInputManifest).validate_python(payload)
    if manifest.manifest_id != manifest_id:
        raise ExperimentInputIndexError("Manifest content identity mismatch")
    team_week = load_historical_team_week_artifact(team_week_path)
    key = TeamWeekKey(
        league_id=team_week.league_id,
        season=team_week.season,
        week=team_week.week,
        roster_id=team_week.roster_id,
    )
    if key != selection.key or team_week.manifest_id != manifest_id:
        raise ExperimentInputIndexError("Selected key or manifest disagrees with the team-week")
    week = next(w for w in league.weeks if w.week == key.week)
    bindings = {
        "league_id": league.league_id,
        "season": league.season,
        "scoring_policy_fingerprint": league.scoring_policy_fingerprint,
        "league_configuration_fingerprint": league.league_configuration_fingerprint,
        "week_boundaries_fingerprint": week.boundaries_fingerprint,
        "builder_version": index.builder_version,
        "projection_config_version": index.projection_config_version,
        "eligibility_policy_version": index.eligibility_policy_version,
    }
    for name, expected in bindings.items():
        if expected is None or getattr(manifest, name) != expected:
            raise ExperimentInputIndexError(f"Unbound or incompatible {name}")
    sources = {source.name: source for source in manifest.source_fingerprints}
    if len(sources) != len(manifest.source_fingerprints):
        raise ExperimentInputIndexError("Manifest source names must be unique")
    for source in league.shared_sources:
        if source.name not in sources or asdict(sources[source.name]) != source.model_dump():
            raise ExperimentInputIndexError(f"Incompatible shared source: {source.name}")
    for player_game in team_week.player_games:
        projection = player_game.projection
        if projection is not None and (
            projection.model_version != manifest.projection_config_version
            or projection.scoring_policy_version != manifest.scoring_policy_version
        ):
            raise ExperimentInputIndexError("Projection disagrees with the selected manifest")
    coverage = team_week.coverage
    accounted = len(team_week.player_games) == coverage.expected_player_games and all(
        value == coverage.expected_player_games
        for value in (
            coverage.joined_player_games,
            coverage.resolved_identities,
            coverage.scored_player_games,
            coverage.projected_player_games,
            coverage.exact_eligibility + coverage.best_known_eligibility,
        )
    )
    status = "assembled"
    if team_week.exclusions or coverage.missing_evidence or not accounted:
        status = "incomplete"
    elif not team_week.player_games:
        status = "empty"
    return {
        "status": status,
        "manifest_id": manifest_id,
        "strict_complete": team_week.complete,
        "coverage": asdict(coverage),
        "eligibility_quality": team_week.eligibility_quality.value,
        "exclusions": [asdict(e) for e in team_week.exclusions],
        "player_games": len(team_week.player_games),
    }


def write_input_inventory(index_path: Path, output_root: Path) -> Path:
    """Write content-addressed index and inventory files; never overwrite changed output."""
    index = ExperimentInputIndex.model_validate_json(index_path.read_bytes())
    index = _resolved_index(index, index_path.parent)
    inventory = build_input_inventory(index, base=index_path.parent)
    root = output_root / str(inventory["index_id"])
    for name, value in (
        ("index.json", index.model_dump(mode="json")),
        ("inventory.json", inventory),
    ):
        path = root / name
        encoded = canonical_json_bytes(value)
        if path.exists():
            if path.read_bytes() != encoded:
                raise ExperimentInputIndexError(
                    f"Refusing to overwrite immutable inventory: {path}"
                )
        else:
            atomic_write_bytes(path, encoded)
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate selected input bundles and account for the full sample."
    )
    parser.add_argument("index", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(write_input_inventory(args.index, args.output_root))
    except (ValueError, OSError) as error:
        parser.exit(1, f"Input inventory failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
