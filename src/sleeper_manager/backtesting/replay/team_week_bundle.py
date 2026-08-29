"""Write one immutable historical team-week input bundle.

This is the public orchestration and module-command boundary. Raw evidence and
projection construction live in dedicated sibling modules so their policies
remain independently inspectable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path

from sleeper_manager.backtesting.experiments.data import (
    HistoricalExperimentInputs,
    load_historical_experiment_inputs,
)
from sleeper_manager.backtesting.replay.inputs import (
    HistoricalReplayBuildInput,
    HistoricalTeamWeekInput,
    ReplayInputManifest,
    assemble_historical_team_week_inputs,
    build_replay_input_manifest,
    write_replay_input_bundle,
)
from sleeper_manager.backtesting.replay.league_archive import (
    ArchivedRoster,
    HistoricalLeagueArchive,
)
from sleeper_manager.backtesting.replay.roster_timeline import (
    FantasyWeekBoundary,
    build_fantasy_week_boundaries,
    reconstruct_roster_timeline,
)
from sleeper_manager.backtesting.replay.team_week_projection import (
    APPROXIMATE_FINALIZATION_POLICY_VERSION,
    player_mappings,
    projection_snapshots,
    selected_games_and_box_scores,
)
from sleeper_manager.backtesting.replay.team_week_sources import (
    HistoricalTeamWeekBundleError,
    ensure_sleeper_archive,
    load_selected_archive,
    nba_season_from_archive,
    player_catalog,
    source_fingerprints,
)
from sleeper_manager.projections.direct_baseline import DirectFantasyPointBaseline

_ELIGIBILITY_POLICY_VERSION = "observed-weekly-starters-current-catalog-best-known-v1"


@dataclass(frozen=True, slots=True)
class HistoricalTeamWeekBundleRequest:
    """Identify the one Sleeper roster and Eastern fantasy week to bundle."""

    league_id: str
    roster_id: int
    week: int
    monday: date

    def __post_init__(self) -> None:
        """Reject identities and week anchors that cannot form a canonical bundle."""

        if not self.league_id.strip():
            raise HistoricalTeamWeekBundleError("Historical bundle requires a Sleeper league ID")
        if self.roster_id <= 0 or self.week <= 0:
            raise HistoricalTeamWeekBundleError(
                "Historical bundle roster and week IDs must be positive"
            )
        if self.monday.weekday() != 0:
            raise HistoricalTeamWeekBundleError(
                "Historical bundle weeks must begin on an Eastern Monday"
            )


@dataclass(frozen=True, slots=True)
class HistoricalTeamWeekBundleOutput:
    """Locate the immutable artifact created for one selected team-week."""

    archive_acquired: bool
    bundle_root: Path
    manifest: ReplayInputManifest
    team_week: HistoricalTeamWeekInput

    @property
    def manifest_path(self) -> Path:
        """Return the content-addressed manifest written beside the team-week input."""

        return self.bundle_root / "manifest.json"

    @property
    def team_week_path(self) -> Path:
        """Return the stable path of this output's one roster-week payload."""

        return (
            self.bundle_root
            / "team-weeks"
            / self.team_week.league_id
            / f"week-{self.team_week.week:02d}"
            / f"roster-{self.team_week.roster_id}.json"
        )


def bootstrap_historical_team_week_bundle(
    workspace: Path,
    request: HistoricalTeamWeekBundleRequest,
    *,
    nba_inputs: HistoricalExperimentInputs | None = None,
    now: datetime | None = None,
) -> HistoricalTeamWeekBundleOutput:
    """Acquire selected evidence and persist one deterministic replay-input bundle."""

    retrieved_at = now or datetime.now(UTC)
    if retrieved_at.tzinfo is None:
        raise HistoricalTeamWeekBundleError(
            "Historical bundle retrieval time must be timezone-aware"
        )
    acquired = ensure_sleeper_archive(
        workspace,
        league_id=request.league_id,
        week=request.week,
        retrieved_at=retrieved_at,
    )
    archive = load_selected_archive(
        workspace,
        league_id=request.league_id,
        roster_id=request.roster_id,
        week=request.week,
        fallback_retrieved_at=retrieved_at,
    )
    resolved_inputs = nba_inputs or load_historical_experiment_inputs(
        workspace / "raw",
        seasons=(nba_season_from_archive(archive),),
        retrieved_at=retrieved_at,
    )
    return _write_team_week_bundle(workspace, request, archive, resolved_inputs, acquired)


def _write_team_week_bundle(
    workspace: Path,
    request: HistoricalTeamWeekBundleRequest,
    archive: HistoricalLeagueArchive,
    nba_inputs: HistoricalExperimentInputs,
    archive_acquired: bool,
) -> HistoricalTeamWeekBundleOutput:
    """Assemble the selected roster-week and write it beneath its manifest hash."""

    boundary = _week_boundary(request)
    anchored_archive = _anchor_archive_to_week(archive, request)
    timeline = reconstruct_roster_timeline(
        anchored_archive,
        week_boundaries=(boundary,),
        season_start=boundary.utc_start,
        season_end=boundary.utc_end,
    )
    roster_player_ids = timeline.players_overlapping(
        request.roster_id,
        boundary.utc_start,
        boundary.utc_end,
    )
    mappings = player_mappings(
        roster_player_ids,
        player_catalog(workspace, request.league_id),
        nba_inputs,
    )
    baseline = DirectFantasyPointBaseline()
    projections = projection_snapshots(nba_inputs, mappings, boundary, anchored_archive, baseline)
    games, box_scores = selected_games_and_box_scores(nba_inputs, mappings, boundary)
    inputs = HistoricalReplayBuildInput(
        archive=anchored_archive,
        roster_timeline=timeline,
        week_boundaries=(boundary,),
        games=games,
        box_scores=box_scores,
        player_mappings=mappings,
        scoring_policy=anchored_archive.scoring_policy,
        eligibility_evidence=anchored_archive.player_eligibility,
        projection_snapshots=projections,
        source_fingerprints=source_fingerprints(
            workspace,
            league_id=request.league_id,
            week=request.week,
            nba_inputs=nba_inputs,
            finalization_policy_version=APPROXIMATE_FINALIZATION_POLICY_VERSION,
        ),
        eligibility_policy_version=_ELIGIBILITY_POLICY_VERSION,
        projection_config_version=baseline.config.model_version,
        builder_version="historical-team-week-bundle-v1",
    )
    manifest = build_replay_input_manifest(inputs)
    team_weeks = assemble_historical_team_week_inputs(
        inputs,
        manifest=manifest,
        weeks=(request.week,),
        roster_ids=(request.roster_id,),
    )
    if len(team_weeks) != 1:
        raise HistoricalTeamWeekBundleError(
            "Selected source evidence did not produce exactly one team-week"
        )
    team_week = _with_observed_starter_lock_eligibility(team_weeks[0])
    if not team_week.player_games:
        raise HistoricalTeamWeekBundleError("Selected team-week has no joined player-game evidence")
    bundle_root = write_replay_input_bundle(workspace / "team-week-inputs", manifest, (team_week,))
    return HistoricalTeamWeekBundleOutput(archive_acquired, bundle_root, manifest, team_week)


def _week_boundary(request: HistoricalTeamWeekBundleRequest) -> FantasyWeekBoundary:
    """Create the requested seven-day Eastern boundary from its supplied Monday."""

    return build_fantasy_week_boundaries({request.week: request.monday})[0]


def _anchor_archive_to_week(
    archive: HistoricalLeagueArchive,
    request: HistoricalTeamWeekBundleRequest,
) -> HistoricalLeagueArchive:
    """Use the selected matchup roster as the week-local timeline reconstruction anchor."""

    matchups = tuple(
        matchup
        for matchup in archive.matchup_weeks
        if matchup.week == request.week and matchup.roster_id == request.roster_id
    )
    if len(matchups) != 1:
        raise HistoricalTeamWeekBundleError(
            "Selected Sleeper week must contain exactly one roster matchup"
        )
    matchup = matchups[0]
    return replace(
        archive,
        final_rosters=(ArchivedRoster(request.roster_id, matchup.player_ids, matchup.starter_ids),),
    )


def _with_observed_starter_lock_eligibility(
    team_week: HistoricalTeamWeekInput,
) -> HistoricalTeamWeekInput:
    """Treat only observed weekly starters as best-known Lock-In eligible pending a richer contract.

    Roster timeline membership remains separate from this conservative policy
    permission until the shared replay contract represents both facts.
    """

    observed_starters = set(team_week.observed_starter_ids)
    return replace(
        team_week,
        player_games=tuple(
            replace(
                player_game,
                rostered_at_tipoff=player_game.sleeper_id in observed_starters,
            )
            for player_game in team_week.player_games
        ),
    )


__all__ = (
    "HistoricalTeamWeekBundleError",
    "HistoricalTeamWeekBundleOutput",
    "HistoricalTeamWeekBundleRequest",
    "bootstrap_historical_team_week_bundle",
)
