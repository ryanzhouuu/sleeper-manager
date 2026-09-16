from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
class PlayerTeamObservation:
    """A historical team association, kept separately from selected game outcomes."""

    provider_player_id: str
    team_id: str
    observed_at: datetime
    source: str
    approximate: bool = False

    def __post_init__(self) -> None:
        """Require attributable, timezone-aware evidence for membership reconstruction."""
        if not all(value.strip() for value in (self.provider_player_id, self.team_id, self.source)):
            raise ReplayInputError("Team observations require player, team and source identities")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ReplayInputError("Team observation time must be timezone-aware")
        if not isinstance(self.approximate, bool):
            raise ReplayInputError("Team observation approximation must be boolean")


@dataclass(frozen=True, slots=True)
class GameRosterCensus:
    """A complete official game-roster review for an explicit player cohort."""

    game_id: str
    home_team_id: str
    away_team_id: str
    reviewed_player_ids: tuple[str, ...]
    player_teams: tuple[tuple[str, str], ...]
    source: str

    def __post_init__(self) -> None:
        """Reject ambiguous associations and undeclared players or teams."""

        if not all(
            value.strip()
            for value in (self.game_id, self.home_team_id, self.away_team_id, self.source)
        ):
            raise ReplayInputError("Game roster censuses require game, teams and source")
        if self.home_team_id == self.away_team_id:
            raise ReplayInputError("Game roster census teams must differ")
        if len(set(self.reviewed_player_ids)) != len(self.reviewed_player_ids):
            raise ReplayInputError("Game roster reviewed players must be unique")
        if any(not player_id.strip() for player_id in self.reviewed_player_ids):
            raise ReplayInputError("Game roster reviewed player IDs must be non-empty")
        player_ids = tuple(player_id for player_id, _ in self.player_teams)
        if len(set(player_ids)) != len(player_ids):
            raise ReplayInputError("Game roster player associations must be unique")
        if not set(player_ids) <= set(self.reviewed_player_ids):
            raise ReplayInputError("Game roster associations require reviewed players")
        teams = {self.home_team_id, self.away_team_id}
        if any(
            not player_id.strip() or team_id not in teams
            for player_id, team_id in self.player_teams
        ):
            raise ReplayInputError("Game roster associations must use participating teams")


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
    eligibility_policy_version: str = "eligibility-v2"
    projection_config_version: str = "projection-unconfigured"
    builder_version: str = "historical-replay-inputs-v3"
    team_observations: tuple[PlayerTeamObservation, ...] = ()
    game_roster_censuses: tuple[GameRosterCensus, ...] = ()

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
        for snapshot in self.projection_snapshots:
            if snapshot.available_as_of.tzinfo is None:
                raise ReplayInputError("Projection availability must be timezone-aware")
            if snapshot.scoring_policy_version != self.scoring_policy.version:
                raise ReplayInputError(
                    "Projection snapshot scoring policy does not match replay scoring"
                )
            if (
                self.projection_config_version != "projection-unconfigured"
                and snapshot.model_version != self.projection_config_version
            ):
                raise ReplayInputError(
                    "Projection snapshot model does not match replay projection configuration"
                )
        census_games = tuple(census.game_id for census in self.game_roster_censuses)
        if len(set(census_games)) != len(census_games):
            raise ReplayInputError("Game roster censuses cannot duplicate a game")
        games = {game.provider_id: game for game in self.games}
        for census in self.game_roster_censuses:
            game = games.get(census.game_id)
            if game is None or (
                census.home_team_id,
                census.away_team_id,
            ) != (game.home_team_id, game.away_team_id):
                raise ReplayInputError("Game roster census conflicts with the schedule")
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
    inferred_team_membership: int = 0

    def __post_init__(self) -> None:
        for label, value in (
            ("expected player-games", self.expected_player_games),
            ("joined player-games", self.joined_player_games),
            ("resolved identities", self.resolved_identities),
            ("exact eligibility", self.exact_eligibility),
            ("best-known eligibility", self.best_known_eligibility),
            ("scored player-games", self.scored_player_games),
            ("projected player-games", self.projected_player_games),
            ("inferred team membership", self.inferred_team_membership),
        ):
            if value < 0:
                raise ReplayInputError(f"{label} counts must be non-negative")
        if self.inferred_team_membership > self.expected_player_games:
            raise ReplayInputError("Inferred membership cannot exceed expected player-games")
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
            and not self.inferred_team_membership
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
    observed_starter_ids: tuple[str | None, ...]
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
    "GameRosterCensus",
    "HistoricalReplayBuildInput",
    "HistoricalTeamWeekInput",
    "PlayerTeamObservation",
    "ReplayCoverageSummary",
    "ReplayInputError",
    "ReplayInputExclusion",
    "ReplayInputManifest",
    "SourceFingerprint",
)
