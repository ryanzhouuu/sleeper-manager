from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from sleeper_manager.backtesting.league_archive import (
    HistoricalLeagueArchive,
    HistoricalMatchup,
    PlayerEligibilitySnapshot,
    atomic_write_json,
)
from sleeper_manager.backtesting.replay_models import (
    ReplayGame,
    ReplayGameStatus,
    ReplayPlayerGame,
)
from sleeper_manager.backtesting.roster_timeline import (
    FantasyWeekBoundary,
    RosterMembershipInterval,
    RosterTimeline,
    assign_game_to_week,
)
from sleeper_manager.domain.nba import GameStatus, PlayerBoxScore, ScheduledGame
from sleeper_manager.domain.planning import PlanningQuality, PlanningReasonCode
from sleeper_manager.domain.scoring import ScoringPolicy, calculate_fantasy_points
from sleeper_manager.integrations.nba.identity import PlayerMapping


class ReplayInputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SourceFingerprint:
    name: str
    content_hash: str
    version: str = "raw-v1"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ReplayInputError("Source fingerprint names must be non-empty")
        if not self.content_hash.strip():
            raise ReplayInputError("Source fingerprints require a content hash")
        if not self.version.strip():
            raise ReplayInputError("Source fingerprint versions must be non-empty")


@dataclass(frozen=True, slots=True)
class HistoricalReplayBuildInput:
    archive: HistoricalLeagueArchive
    roster_timeline: RosterTimeline
    week_boundaries: tuple[FantasyWeekBoundary, ...]
    games: tuple[ScheduledGame, ...]
    box_scores: tuple[PlayerBoxScore, ...]
    player_mappings: tuple[PlayerMapping, ...]
    scoring_policy: ScoringPolicy
    eligibility_evidence: tuple[PlayerEligibilitySnapshot, ...] = ()
    source_fingerprints: tuple[SourceFingerprint, ...] = ()
    eligibility_policy_version: str = "eligibility-v1"
    projection_config_version: str = "projection-unconfigured"
    builder_version: str = "historical-replay-inputs-v1"

    def __post_init__(self) -> None:
        if self.archive.scoring_policy.fingerprint != self.scoring_policy.fingerprint:
            raise ReplayInputError(
                "Replay scoring policy does not match the archived league policy"
            )
        if not self.week_boundaries:
            raise ReplayInputError("Historical replay inputs require fantasy-week boundaries")
        for label, value in (
            ("eligibility policy version", self.eligibility_policy_version),
            ("projection configuration version", self.projection_config_version),
            ("builder version", self.builder_version),
        ):
            if not value.strip():
                raise ReplayInputError(f"{label} must be non-empty")
        _unique_source_names(self.source_fingerprints)


@dataclass(frozen=True, slots=True)
class ReplayInputManifest:
    league_id: str
    season: str
    builder_version: str
    source_fingerprints: tuple[SourceFingerprint, ...]
    scoring_policy_version: str
    scoring_policy_fingerprint: str
    league_configuration_fingerprint: str
    roster_timeline_fingerprint: str
    week_boundaries_fingerprint: str
    eligibility_policy_version: str
    projection_config_version: str

    @property
    def manifest_id(self) -> str:
        return _sha256_text(_canonical_json(self))

    def to_dict(self) -> dict[str, Any]:
        payload = _canonical(self)
        assert isinstance(payload, dict)
        payload["manifest_id"] = self.manifest_id
        return payload


@dataclass(frozen=True, slots=True)
class ReplayInputExclusion:
    reason: PlanningReasonCode
    scope: str
    detail: str

    def __post_init__(self) -> None:
        if not self.scope.strip():
            raise ReplayInputError("Replay exclusions require a scope")
        if not self.detail.strip():
            raise ReplayInputError("Replay exclusions require a detail")


@dataclass(frozen=True, slots=True)
class ReplayCoverageSummary:
    expected_player_games: int
    joined_player_games: int
    resolved_identities: int
    exact_eligibility: int
    best_known_eligibility: int
    scored_player_games: int
    missing_evidence: tuple[tuple[PlanningReasonCode, int], ...] = ()

    def __post_init__(self) -> None:
        for label, value in (
            ("expected player-games", self.expected_player_games),
            ("joined player-games", self.joined_player_games),
            ("resolved identities", self.resolved_identities),
            ("exact eligibility", self.exact_eligibility),
            ("best-known eligibility", self.best_known_eligibility),
            ("scored player-games", self.scored_player_games),
        ):
            if value < 0:
                raise ReplayInputError(f"{label} counts must be non-negative")
        keys = tuple(reason for reason, _ in self.missing_evidence)
        if len(set(keys)) != len(keys):
            raise ReplayInputError("Coverage missing-evidence reasons must be unique")
        if any(count < 0 for _, count in self.missing_evidence):
            raise ReplayInputError("Coverage missing-evidence counts must be non-negative")

    @property
    def complete(self) -> bool:
        return (
            self.expected_player_games == self.joined_player_games
            and self.expected_player_games == self.resolved_identities
            and self.exact_eligibility == self.expected_player_games
            and self.scored_player_games == self.expected_player_games
            and not self.missing_evidence
        )


@dataclass(frozen=True, slots=True)
class HistoricalTeamWeekInput:
    manifest_id: str
    league_id: str
    season: str
    week: int
    roster_id: int
    starter_slots: tuple[str, ...]
    roster_player_ids: tuple[str, ...]
    observed_starter_ids: tuple[str, ...]
    games: tuple[ReplayGame, ...]
    player_games: tuple[ReplayPlayerGame, ...]
    eligibility_quality: PlanningQuality
    coverage: ReplayCoverageSummary
    exclusions: tuple[ReplayInputExclusion, ...] = ()

    @property
    def complete(self) -> bool:
        return self.coverage.complete and not self.exclusions

    def to_dict(self) -> dict[str, Any]:
        payload = _canonical(self)
        assert isinstance(payload, dict)
        payload["complete"] = self.complete
        return payload


@dataclass(frozen=True, slots=True)
class _JoinedPlayerGame:
    sleeper_id: str
    mapping: PlayerMapping
    game: ScheduledGame
    box_score: PlayerBoxScore
    roster_id: int
    membership_segment: str | None


def assemble_historical_team_week_inputs(
    inputs: HistoricalReplayBuildInput,
    *,
    manifest: ReplayInputManifest | None = None,
    weeks: Sequence[int] | None = None,
    roster_ids: Sequence[int] | None = None,
) -> tuple[HistoricalTeamWeekInput, ...]:
    """Join immutable league, roster, schedule, and box-score evidence by tipoff."""

    replay_manifest = manifest or build_replay_input_manifest(inputs)
    boundaries = tuple(
        boundary
        for boundary in inputs.week_boundaries
        if weeks is None or boundary.week in set(weeks)
    )
    selected_rosters = _roster_ids(inputs, roster_ids)
    schedule_by_id, schedule_issues = _index_schedules(inputs.games)
    box_scores, box_score_issues = _index_box_scores(inputs.box_scores)
    mapping_by_provider, mapping_issues = _index_mappings(inputs.player_mappings)
    global_issues = [*schedule_issues, *box_score_issues, *mapping_issues]

    games_by_week: dict[int, tuple[ReplayGame, ...]] = {}
    for boundary in boundaries:
        games_by_week[boundary.week] = tuple(
            _replay_game(game, boundary.week)
            for game in schedule_by_id.values()
            if boundary.utc_start <= game.start_time < boundary.utc_end
        )

    joined_by_scope: dict[tuple[int, int], list[_JoinedPlayerGame]] = defaultdict(list)
    for box_score in box_scores:
        game = schedule_by_id.get(box_score.game_id)
        if game is None:
            global_issues.append(
                ReplayInputExclusion(
                    PlanningReasonCode.MISSING_GAME_SCHEDULE,
                    f"game={box_score.game_id}",
                    "Player box score has no matching historical schedule.",
                )
            )
            continue
        game_week = assign_game_to_week(game.start_time, boundaries)
        if game_week is None:
            continue
        mapping = mapping_by_provider.get(box_score.player_id)
        if mapping is None:
            global_issues.append(
                ReplayInputExclusion(
                    PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY,
                    f"provider-player={box_score.player_id}",
                    "No Sleeper-to-provider identity mapping was supplied.",
                )
            )
            continue
        sleeper_id = mapping.sleeper_id
        for roster_id in selected_rosters:
            interval = _membership_interval(
                inputs.roster_timeline, roster_id, sleeper_id, game.start_time
            )
            if interval is None:
                continue
            joined_by_scope[(game_week.week, roster_id)].append(
                _JoinedPlayerGame(
                    sleeper_id,
                    mapping,
                    game,
                    box_score,
                    roster_id,
                    _membership_segment(interval),
                )
            )

    global_missing = _reason_counts(global_issues)
    team_weeks: list[HistoricalTeamWeekInput] = []
    for boundary in boundaries:
        for roster_id in selected_rosters:
            scope = f"league={inputs.archive.league_id}:week={boundary.week}:roster={roster_id}"
            scope_issues = list(global_issues)
            candidates = joined_by_scope.get((boundary.week, roster_id), [])
            player_games: list[ReplayPlayerGame] = []
            exact_count = 0
            best_known_count = 0
            scored_count = 0
            missing_evidence: dict[PlanningReasonCode, int] = defaultdict(int)
            for candidate in candidates:
                positions, quality = _eligibility_at(
                    candidate.sleeper_id,
                    candidate.game.start_time,
                    inputs.eligibility_evidence or inputs.archive.player_eligibility,
                )
                if not positions:
                    reason = PlanningReasonCode.AMBIGUOUS_ELIGIBILITY
                    missing_evidence[reason] += 1
                    scope_issues.append(
                        ReplayInputExclusion(
                            reason,
                            scope,
                            "No unambiguous eligibility evidence exists at "
                            f"{candidate.game.start_time.isoformat()}.",
                        )
                    )
                    continue
                if quality is PlanningQuality.EXACT:
                    exact_count += 1
                else:
                    best_known_count += 1
                score = calculate_fantasy_points(candidate.box_score.line, inputs.scoring_policy)
                scored_count += 1
                player_games.append(
                    ReplayPlayerGame(
                        sleeper_id=candidate.sleeper_id,
                        provider_player_id=candidate.mapping.espn_id or "",
                        game_id=candidate.game.provider_id,
                        fantasy_team_id=roster_id,
                        rostered_at_tipoff=True,
                        eligible_positions=positions,
                        actual_score=score,
                        membership_segment=candidate.membership_segment,
                    )
                )

            for issue in scope_issues:
                missing_evidence[issue.reason] += 1
            coverage = ReplayCoverageSummary(
                expected_player_games=len(candidates) + sum(global_missing.values()),
                joined_player_games=len(candidates),
                resolved_identities=len(candidates),
                exact_eligibility=exact_count,
                best_known_eligibility=best_known_count,
                scored_player_games=scored_count,
                missing_evidence=tuple(
                    sorted(missing_evidence.items(), key=lambda item: item[0].value)
                ),
            )
            quality = _assembly_quality(coverage, scope_issues)
            excluded = _unique_exclusions(scope_issues)
            if excluded:
                player_games = []
            team_weeks.append(
                HistoricalTeamWeekInput(
                    manifest_id=replay_manifest.manifest_id,
                    league_id=inputs.archive.league_id,
                    season=inputs.archive.season,
                    week=boundary.week,
                    roster_id=roster_id,
                    starter_slots=_starter_slots(inputs.archive.roster_slots),
                    roster_player_ids=_roster_players_for_week(
                        inputs.roster_timeline, roster_id, boundary
                    ),
                    observed_starter_ids=_observed_starters(
                        inputs.archive.matchup_weeks, boundary.week, roster_id
                    ),
                    games=games_by_week.get(boundary.week, ()),
                    player_games=tuple(player_games),
                    eligibility_quality=quality,
                    coverage=coverage,
                    exclusions=excluded,
                )
            )
    return tuple(team_weeks)


def _index_schedules(
    games: Sequence[ScheduledGame],
) -> tuple[dict[str, ScheduledGame], tuple[ReplayInputExclusion, ...]]:
    indexed: dict[str, ScheduledGame] = {}
    issues: list[ReplayInputExclusion] = []
    for game in games:
        previous = indexed.get(game.provider_id)
        if previous is None:
            indexed[game.provider_id] = game
            continue
        if previous != game:
            issues.append(
                ReplayInputExclusion(
                    PlanningReasonCode.AMBIGUOUS_EVENT_ORDER,
                    f"game={game.provider_id}",
                    "Historical schedule contains conflicting rows for one game ID.",
                )
            )
    return indexed, tuple(issues)


def _index_box_scores(
    box_scores: Sequence[PlayerBoxScore],
) -> tuple[tuple[PlayerBoxScore, ...], tuple[ReplayInputExclusion, ...]]:
    indexed: dict[tuple[str, str], PlayerBoxScore] = {}
    issues: list[ReplayInputExclusion] = []
    for box_score in box_scores:
        key = box_score.game_id, box_score.player_id
        previous = indexed.get(key)
        if previous is None:
            indexed[key] = box_score
            continue
        if previous != box_score:
            issues.append(
                ReplayInputExclusion(
                    PlanningReasonCode.AMBIGUOUS_EVENT_ORDER,
                    f"game={box_score.game_id}:provider-player={box_score.player_id}",
                    "Historical box scores contain conflicting rows for one player-game.",
                )
            )
    return tuple(indexed.values()), tuple(issues)


def _index_mappings(
    mappings: Sequence[PlayerMapping],
) -> tuple[dict[str, PlayerMapping], tuple[ReplayInputExclusion, ...]]:
    by_provider: dict[str, list[PlayerMapping]] = defaultdict(list)
    issues: list[ReplayInputExclusion] = []
    for mapping in mappings:
        if mapping.espn_id is None:
            issues.append(
                ReplayInputExclusion(
                    PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY,
                    f"sleeper-player={mapping.sleeper_id}",
                    mapping.reason,
                )
            )
            continue
        by_provider[mapping.espn_id].append(mapping)

    resolved: dict[str, PlayerMapping] = {}
    for provider_id, candidates in by_provider.items():
        unique = tuple(dict.fromkeys(candidates))
        if len({candidate.sleeper_id for candidate in unique}) != 1:
            issues.append(
                ReplayInputExclusion(
                    PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY,
                    f"provider-player={provider_id}",
                    "Provider player ID maps to multiple Sleeper players.",
                )
            )
            continue
        resolved[provider_id] = unique[0]
    return resolved, tuple(issues)


def _replay_game(game: ScheduledGame, week: int) -> ReplayGame:
    status = {
        GameStatus.SCHEDULED: ReplayGameStatus.SCHEDULED,
        GameStatus.IN_PROGRESS: ReplayGameStatus.SCHEDULED,
        GameStatus.FINAL: ReplayGameStatus.FINAL,
        GameStatus.POSTPONED: ReplayGameStatus.POSTPONED,
        GameStatus.CANCELED: ReplayGameStatus.CANCELED,
        GameStatus.UNKNOWN: ReplayGameStatus.SCHEDULED,
    }[game.status]
    final_time = (
        game.start_time + timedelta(minutes=game.duration_minutes)
        if status is ReplayGameStatus.FINAL
        else None
    )
    return ReplayGame(
        game_id=game.provider_id,
        start_time=game.start_time,
        final_time=final_time,
        week=week,
        team_ids=(game.home_team_id, game.away_team_id),
        status=status,
    )


def _eligibility_at(
    sleeper_id: str,
    cutoff: datetime,
    evidence: Sequence[PlayerEligibilitySnapshot],
) -> tuple[tuple[str, ...], PlanningQuality]:
    snapshots = tuple(
        sorted(
            (snapshot for snapshot in evidence if snapshot.sleeper_id == sleeper_id),
            key=lambda snapshot: snapshot.available_as_of,
        )
    )
    exact = tuple(snapshot for snapshot in snapshots if snapshot.available_as_of <= cutoff)
    if exact:
        snapshot = exact[-1]
        same_time = tuple(
            item for item in exact if item.available_as_of == snapshot.available_as_of
        )
        positions = _consistent_positions(same_time)
        return positions, PlanningQuality.EXACT if positions else PlanningQuality.PARTIAL
    if snapshots:
        positions = _consistent_positions((snapshots[0],))
        return (
            positions,
            PlanningQuality.BEST_KNOWN_CONSTRAINTS_ORACLE if positions else PlanningQuality.PARTIAL,
        )
    return (), PlanningQuality.PARTIAL


def _consistent_positions(
    snapshots: Sequence[PlayerEligibilitySnapshot],
) -> tuple[str, ...]:
    position_sets = {
        tuple(
            sorted(
                {
                    position.strip().upper()
                    for position in snapshot.eligible_positions
                    if position.strip()
                }
            )
        )
        for snapshot in snapshots
    }
    if len(position_sets) != 1:
        return ()
    return next(iter(position_sets), ())


def _membership_interval(
    timeline: RosterTimeline,
    roster_id: int,
    sleeper_id: str,
    at: datetime,
) -> RosterMembershipInterval | None:
    matches = tuple(
        interval
        for interval in timeline.intervals
        if interval.roster_id == roster_id
        and interval.sleeper_player_id == sleeper_id
        and interval.starts_at <= at < interval.ends_at
    )
    return matches[0] if len(matches) == 1 else None


def _membership_segment(interval: RosterMembershipInterval) -> str:
    return (
        ":".join(interval.source_transaction_ids) if interval.source_transaction_ids else "initial"
    )


def _roster_ids(
    inputs: HistoricalReplayBuildInput,
    requested: Sequence[int] | None,
) -> tuple[int, ...]:
    discovered = {roster.roster_id for roster in inputs.archive.final_rosters}
    discovered.update(interval.roster_id for interval in inputs.roster_timeline.intervals)
    discovered.update(matchup.roster_id for matchup in inputs.archive.matchup_weeks)
    selected = discovered if requested is None else discovered.intersection(requested)
    return tuple(sorted(roster_id for roster_id in selected if roster_id > 0))


def _roster_players_for_week(
    timeline: RosterTimeline,
    roster_id: int,
    boundary: FantasyWeekBoundary,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                interval.sleeper_player_id
                for interval in timeline.intervals
                if interval.roster_id == roster_id
                and interval.starts_at < boundary.utc_end
                and interval.ends_at > boundary.utc_start
            }
        )
    )


def _observed_starters(
    matchups: Sequence[HistoricalMatchup],
    week: int,
    roster_id: int,
) -> tuple[str, ...]:
    matching = tuple(
        matchup for matchup in matchups if matchup.week == week and matchup.roster_id == roster_id
    )
    if not matching:
        return ()
    return tuple(dict.fromkeys(matching[0].starter_ids))


def _starter_slots(roster_slots: Sequence[str]) -> tuple[str, ...]:
    reserve_slots = frozenset({"BN", "IR", "IR+", "RES", "TAXI"})
    return tuple(
        slot.strip().upper()
        for slot in roster_slots
        if slot.strip() and slot.strip().upper() not in reserve_slots
    )


def _reason_counts(issues: Sequence[ReplayInputExclusion]) -> dict[PlanningReasonCode, int]:
    counts: dict[PlanningReasonCode, int] = defaultdict(int)
    for issue in issues:
        counts[issue.reason] += 1
    return dict(counts)


def _assembly_quality(
    coverage: ReplayCoverageSummary,
    exclusions: Sequence[ReplayInputExclusion],
) -> PlanningQuality:
    if exclusions:
        return PlanningQuality.PARTIAL
    if coverage.complete:
        return PlanningQuality.EXACT
    if coverage.best_known_eligibility:
        return PlanningQuality.BEST_KNOWN_CONSTRAINTS_ORACLE
    return PlanningQuality.PARTIAL


def _unique_exclusions(
    exclusions: Sequence[ReplayInputExclusion],
) -> tuple[ReplayInputExclusion, ...]:
    return tuple(dict.fromkeys(exclusions))


def source_fingerprint(name: str, payload: bytes, *, version: str = "raw-v1") -> SourceFingerprint:
    return SourceFingerprint(
        name=name, content_hash=hashlib.sha256(payload).hexdigest(), version=version
    )


def records_fingerprint(
    name: str, records: Sequence[object], *, version: str = "records-v1"
) -> SourceFingerprint:
    return SourceFingerprint(name, _sha256_text(_canonical_json(tuple(records))), version)


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
    manifest_path = bundle_root / "manifest.json"
    _write_immutable_json(manifest_path, manifest.to_dict())
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
    encoded = _canonical_json(payload).encode()
    if path.is_file():
        if path.read_bytes() != encoded:
            raise ReplayInputError(f"Refusing to overwrite immutable replay input: {path}")
        return
    atomic_write_json(path, json.loads(encoded))


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


def _unique_source_names(fingerprints: Sequence[SourceFingerprint]) -> None:
    names = tuple(fingerprint.name for fingerprint in fingerprints)
    if len(set(names)) != len(names):
        raise ReplayInputError("Source fingerprint names must be unique")


def _fingerprint_value(value: object) -> str:
    return _sha256_text(_canonical_json(value))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"))


def _canonical(value: object) -> Any:
    if isinstance(value, Enum):
        return _canonical(value.value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if is_dataclass(value):
        return {field.name: _canonical(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


__all__ = (
    "HistoricalReplayBuildInput",
    "HistoricalTeamWeekInput",
    "ReplayCoverageSummary",
    "ReplayInputError",
    "ReplayInputExclusion",
    "ReplayInputManifest",
    "SourceFingerprint",
    "build_replay_input_manifest",
    "records_fingerprint",
    "source_fingerprint",
    "write_replay_input_bundle",
)
