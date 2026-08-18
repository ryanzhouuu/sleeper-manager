from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from sleeper_manager.backtesting.artifacts import (
    atomic_write_bytes,
    canonical_json,
    canonical_json_bytes,
    sha256_bytes,
    sha256_text,
)
from sleeper_manager.backtesting.replay.inputs.models import (
    HistoricalReplayBuildInput,
    HistoricalTeamWeekInput,
    ReplayInputError,
    ReplayInputManifest,
    SourceFingerprint,
)


def source_fingerprint(name: str, payload: bytes, *, version: str = "raw-v1") -> SourceFingerprint:
    return SourceFingerprint(name=name, content_hash=sha256_bytes(payload), version=version)


def records_fingerprint(
    name: str, records: Sequence[object], *, version: str = "records-v1"
) -> SourceFingerprint:
    return SourceFingerprint(name, sha256_text(canonical_json(tuple(records))), version)


def build_replay_input_manifest(
    inputs: HistoricalReplayBuildInput,
) -> ReplayInputManifest:
    fingerprints = list(inputs.source_fingerprints)
    fingerprints.extend(
        SourceFingerprint(f"sleeper:{artifact.resource}", artifact.content_hash, "archive-v1")
        for artifact in inputs.archive.source_artifacts
    )
    fingerprints.extend(
        (
            records_fingerprint("parsed-schedule", inputs.games),
            records_fingerprint("parsed-box-scores", inputs.box_scores),
            records_fingerprint("player-mappings", inputs.player_mappings),
            records_fingerprint("eligibility-evidence", inputs.eligibility_evidence),
            records_fingerprint("roster-timeline", (inputs.roster_timeline,)),
            records_fingerprint("fantasy-week-boundaries", inputs.week_boundaries),
        )
    )
    normalized_fingerprints = _normalize_fingerprints(fingerprints)
    return ReplayInputManifest(
        league_id=inputs.archive.league_id,
        season=inputs.archive.season,
        builder_version=inputs.builder_version,
        source_fingerprints=normalized_fingerprints,
        scoring_policy_version=inputs.scoring_policy.version,
        scoring_policy_fingerprint=inputs.scoring_policy.fingerprint,
        league_configuration_fingerprint=inputs.archive.configuration_fingerprint,
        roster_timeline_fingerprint=_fingerprint_value(inputs.roster_timeline),
        week_boundaries_fingerprint=_fingerprint_value(inputs.week_boundaries),
        eligibility_policy_version=inputs.eligibility_policy_version,
        projection_config_version=inputs.projection_config_version,
    )


def write_replay_input_bundle(
    root: Path,
    manifest: ReplayInputManifest,
    team_weeks: Sequence[HistoricalTeamWeekInput],
) -> Path:
    bundle_root = root / manifest.manifest_id
    _write_immutable_json(bundle_root / "manifest.json", manifest.to_dict())
    for team_week in team_weeks:
        if team_week.manifest_id != manifest.manifest_id:
            raise ReplayInputError("Team-week input belongs to a different manifest")
        path = (
            bundle_root
            / "team-weeks"
            / team_week.league_id
            / f"week-{team_week.week:02d}"
            / f"roster-{team_week.roster_id}.json"
        )
        _write_immutable_json(path, team_week.to_dict())
    return bundle_root


def _write_immutable_json(path: Path, payload: object) -> None:
    encoded = canonical_json_bytes(payload)
    if path.is_file():
        if path.read_bytes() != encoded:
            raise ReplayInputError(f"Refusing to overwrite immutable replay input: {path}")
        return
    atomic_write_bytes(path, encoded)


def _normalize_fingerprints(
    fingerprints: Sequence[SourceFingerprint],
) -> tuple[SourceFingerprint, ...]:
    by_name: dict[str, SourceFingerprint] = {}
    for fingerprint in fingerprints:
        previous = by_name.get(fingerprint.name)
        if previous is not None and previous != fingerprint:
            raise ReplayInputError(
                f"Conflicting fingerprints were supplied for source {fingerprint.name!r}"
            )
        by_name[fingerprint.name] = fingerprint
    return tuple(by_name[name] for name in sorted(by_name))


def _fingerprint_value(value: object) -> str:
    return sha256_text(canonical_json(value))


__all__ = (
    "build_replay_input_manifest",
    "records_fingerprint",
    "source_fingerprint",
    "write_replay_input_bundle",
)
