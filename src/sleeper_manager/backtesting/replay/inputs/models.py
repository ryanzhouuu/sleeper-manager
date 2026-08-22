from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sleeper_manager.backtesting.artifacts import canonical_json, canonicalize, sha256_text
from sleeper_manager.backtesting.replay.league_archive import (
    HistoricalLeagueArchive,
    PlayerEligibilitySnapshot,
)
from sleeper_manager.backtesting.replay.models import ReplayGame, ReplayPlayerGame
from sleeper_manager.backtesting.replay.roster_timeline import (
    FantasyWeekBoundary,
    RosterTimeline,
)
from sleeper_manager.domain.nba import PlayerBoxScore, ScheduledGame
from sleeper_manager.domain.planning import PlanningQuality, PlanningReasonCode
from sleeper_manager.domain.projection import ProjectionSnapshot
from sleeper_manager.domain.scoring import ScoringPolicy
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
    projection_snapshots: tuple[ProjectionSnapshot, ...] = ()
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
        projection_keys = tuple(
            (snapshot.player_id, snapshot.game_id) for snapshot in self.projection_snapshots
        )
        if len(set(projection_keys)) != len(projection_keys):
            raise ReplayInputError("Projection snapshots cannot duplicate a player-game")
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
        return sha256_text(canonical_json(self))

    def to_dict(self) -> dict[str, Any]:
        payload = canonicalize(self)
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
    projected_player_games: int = 0

    def __post_init__(self) -> None:
        for label, value in (
            ("expected player-games", self.expected_player_games),
            ("joined player-games", self.joined_player_games),
            ("resolved identities", self.resolved_identities),
            ("exact eligibility", self.exact_eligibility),
            ("best-known eligibility", self.best_known_eligibility),
            ("scored player-games", self.scored_player_games),
            ("projected player-games", self.projected_player_games),
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
            and self.projected_player_games == self.expected_player_games
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
        payload = canonicalize(self)
        assert isinstance(payload, dict)
        payload["complete"] = self.complete
        return payload


def _unique_source_names(fingerprints: tuple[SourceFingerprint, ...]) -> None:
    names = tuple(fingerprint.name for fingerprint in fingerprints)
    if len(set(names)) != len(names):
        raise ReplayInputError("Source fingerprint names must be unique")


__all__ = (
    "HistoricalReplayBuildInput",
    "HistoricalTeamWeekInput",
    "ReplayCoverageSummary",
    "ReplayInputError",
    "ReplayInputExclusion",
    "ReplayInputManifest",
    "SourceFingerprint",
)
