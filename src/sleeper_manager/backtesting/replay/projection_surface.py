"""Build, validate, and select historical cutoff projection surfaces.

Full-advisor replay consumes these sidecars instead of the single near-tipoff
projection retained in each historical bundle for restricted diagnostics.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from sleeper_manager.backtesting.artifacts import canonical_json, sha256_text
from sleeper_manager.backtesting.experiments.data import (
    HistoricalExperimentInputs,
    dataset_version_for,
)
from sleeper_manager.backtesting.replay.inputs.models import (
    HistoricalTeamWeekInput,
    SourceFingerprint,
)
from sleeper_manager.backtesting.replay.models import ReplayPlayerGame
from sleeper_manager.backtesting.replay.projection_surface_models import (
    FULL_ADVISOR_CUTOFF_SCHEDULE_VERSION,
    PROJECTION_SURFACE_SCHEMA_VERSION,
    HistoricalProjectionSurface,
    HistoricalProjectionSurfaceError,
    ProjectionSurfaceEntry,
)
from sleeper_manager.backtesting.replay.runner import (
    ReplayEvent,
    ReplayEventKind,
    build_chronological_events,
)
from sleeper_manager.backtesting.replay.state import ReplayState
from sleeper_manager.backtesting.replay.team_week_projection import (
    APPROXIMATE_FINALIZATION_POLICY_VERSION,
    approximate_finalization_at,
)
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_models import FEATURE_SCHEMA_VERSION
from sleeper_manager.projections.direct_baseline import (
    DirectBaselineObservation,
    DirectFantasyPointBaseline,
    PregameProjectionRequest,
    ProjectionBaselineError,
)


def team_week_fingerprint(team_week: HistoricalTeamWeekInput) -> str:
    """Identify the decoded team-week independently of its filesystem encoding."""

    return sha256_text(canonical_json(team_week))


def full_advisor_planning_cutoffs(
    team_week: HistoricalTeamWeekInput,
    planning_lead_time: timedelta = timedelta(minutes=10),
) -> tuple[datetime, ...]:
    """Derive the exact lineup refresh times used by full-advisor replay."""

    events = full_advisor_events(team_week, planning_lead_time)
    return tuple(event.at for event in events if event.kind is ReplayEventKind.PLANNING_CUTOFF)


def full_advisor_events(
    team_week: HistoricalTeamWeekInput,
    planning_lead_time: timedelta = timedelta(minutes=10),
) -> tuple[ReplayEvent, ...]:
    """Build the shared chronological schedule for surfaces and execution."""

    state = ReplayState(team_week.starter_slots, team_week.games, team_week.player_games)
    return build_chronological_events(
        state,
        planning_lead_time=planning_lead_time + timedelta(microseconds=1),
    )


def build_historical_projection_surface(
    team_week: HistoricalTeamWeekInput,
    nba_inputs: HistoricalExperimentInputs,
    baseline: DirectFantasyPointBaseline,
    *,
    scoring_policy: ScoringPolicy,
    planning_lead_time: timedelta = timedelta(minutes=10),
) -> HistoricalProjectionSurface:
    """Generate every required cutoff attempt without admitting future outcomes."""

    games = {game.game_id: game for game in team_week.games}
    history_games = {game.provider_id: game for game in nba_inputs.games}
    source_version = _player_box_source_version(nba_inputs)
    history = tuple(
        DirectBaselineObservation(
            player_id=box.player_id,
            game_id=box.game_id,
            game_start=history_games[box.game_id].start_time,
            outcome_finalized_at=approximate_finalization_at(history_games[box.game_id].start_time),
            minutes=box.minutes,
            started=box.started,
            did_not_play=not box.did_play,
            box_score=box.line,
            source_version=source_version,
        )
        for box in nba_inputs.player_box_scores
        if box.game_id in history_games
    )
    dataset_version = dataset_version_for(
        nba_inputs.artifacts,
        scoring_policy=scoring_policy,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
    )
    entries: list[ProjectionSurfaceEntry] = []
    for cutoff in full_advisor_planning_cutoffs(team_week, planning_lead_time):
        for player_game in team_week.player_games:
            game = games[player_game.game_id]
            if game.start_time <= cutoff:
                continue
            request = PregameProjectionRequest(
                dataset_version=dataset_version,
                feature_schema_version=FEATURE_SCHEMA_VERSION,
                player_id=player_game.sleeper_id,
                history_player_id=player_game.provider_player_id,
                game_id=player_game.game_id,
                game_start=game.start_time,
                available_as_of=cutoff,
                history=history,
            )
            try:
                projection = baseline.project_pregame(request, scoring_policy=scoring_policy)
            except ProjectionBaselineError as error:
                entries.append(
                    ProjectionSurfaceEntry(
                        cutoff,
                        player_game.sleeper_id,
                        player_game.game_id,
                        failure_reason=error.reason_code,
                        failure_detail=str(error),
                    )
                )
            else:
                entries.append(
                    ProjectionSurfaceEntry(
                        cutoff,
                        player_game.sleeper_id,
                        player_game.game_id,
                        projection=projection,
                    )
                )
    return HistoricalProjectionSurface(
        team_week_manifest_id=team_week.manifest_id,
        league_id=team_week.league_id,
        season=team_week.season,
        week=team_week.week,
        roster_id=team_week.roster_id,
        team_week_fingerprint=team_week_fingerprint(team_week),
        projection_config_version=baseline.config.model_version,
        scoring_policy_version=scoring_policy.version,
        source_fingerprints=_source_fingerprints(nba_inputs),
        entries=tuple(entries),
        planning_lead_time_seconds=planning_lead_time.total_seconds(),
        finalization_policy_version=APPROXIMATE_FINALIZATION_POLICY_VERSION,
    )


def validate_historical_projection_surface(
    surface: HistoricalProjectionSurface,
    *,
    team_week: HistoricalTeamWeekInput,
    projection_config_version: str | None = None,
    planning_lead_time: timedelta | None = None,
) -> None:
    """Require exact team-week, cutoff, model, scoring, and key compatibility."""

    identities = (
        (surface.team_week_manifest_id, team_week.manifest_id, "manifest"),
        (surface.league_id, team_week.league_id, "league"),
        (surface.season, team_week.season, "season"),
        (surface.week, team_week.week, "week"),
        (surface.roster_id, team_week.roster_id, "roster"),
    )
    for actual_identity, expected_identity, label in identities:
        if actual_identity != expected_identity:
            raise HistoricalProjectionSurfaceError(f"Surface {label} disagrees with team-week")
    if surface.team_week_fingerprint != team_week_fingerprint(team_week):
        raise HistoricalProjectionSurfaceError("Surface team-week fingerprint mismatch")
    if surface.cutoff_schedule_version != FULL_ADVISOR_CUTOFF_SCHEDULE_VERSION:
        raise HistoricalProjectionSurfaceError("Surface cutoff schedule version mismatch")
    if surface.finalization_policy_version != APPROXIMATE_FINALIZATION_POLICY_VERSION:
        raise HistoricalProjectionSurfaceError("Surface finalization policy version mismatch")
    if (
        projection_config_version is not None
        and surface.projection_config_version != projection_config_version
    ):
        raise HistoricalProjectionSurfaceError("Surface projection configuration mismatch")
    lead_time = planning_lead_time or timedelta(seconds=surface.planning_lead_time_seconds)
    if lead_time.total_seconds() != surface.planning_lead_time_seconds:
        raise HistoricalProjectionSurfaceError("Surface planning lead time mismatch")
    expected_keys = _required_keys(team_week, full_advisor_planning_cutoffs(team_week, lead_time))
    actual_keys = {entry.key for entry in surface.entries}
    if missing := expected_keys - actual_keys:
        raise HistoricalProjectionSurfaceError(
            f"Surface is missing required entries: {_format_keys(missing)}"
        )
    if unexpected := actual_keys - expected_keys:
        raise HistoricalProjectionSurfaceError(
            f"Surface has unexpected entries: {_format_keys(unexpected)}"
        )
    games = {game.game_id: game for game in team_week.games}
    for entry in surface.entries:
        if entry.decision_time >= games[entry.game_id].start_time:
            raise HistoricalProjectionSurfaceError("Surface projection is not pre-tipoff")
        projection = entry.projection
        if projection is not None and (
            projection.model_version != surface.projection_config_version
            or projection.scoring_policy_version != surface.scoring_policy_version
        ):
            raise HistoricalProjectionSurfaceError("Surface projection version mismatch")


def player_games_with_projection_surface(
    player_games: tuple[ReplayPlayerGame, ...],
    surface: HistoricalProjectionSurface,
    *,
    game_starts: dict[str, datetime],
    decision_time: datetime,
    exact_cutoff: bool,
) -> tuple[ReplayPlayerGame, ...]:
    """Overlay the newest legal sidecar snapshot without bundle fallback."""

    by_player_game: dict[tuple[str, str], list[ProjectionSurfaceEntry]] = {}
    for entry in surface.entries:
        by_player_game.setdefault((entry.player_id, entry.game_id), []).append(entry)
    projected: list[ReplayPlayerGame] = []
    for player_game in player_games:
        start = game_starts[player_game.game_id]
        entries = by_player_game.get((player_game.sleeper_id, player_game.game_id), [])
        if exact_cutoff and start > decision_time:
            candidates = [entry for entry in entries if entry.decision_time == decision_time]
        else:
            upper_bound = min(decision_time, start)
            candidates = [entry for entry in entries if entry.decision_time <= upper_bound]
        if not candidates:
            raise HistoricalProjectionSurfaceError(
                f"projection_surface_missing:{player_game.sleeper_id}:{player_game.game_id}"
            )
        selected = max(candidates, key=lambda entry: entry.decision_time)
        if selected.projection is None:
            raise HistoricalProjectionSurfaceError(
                "projection_surface_failure:"
                f"{selected.player_id}:{selected.game_id}:{selected.failure_reason}"
            )
        projected.append(replace(player_game, projection=selected.projection))
    return tuple(projected)


def _required_keys(
    team_week: HistoricalTeamWeekInput, cutoffs: tuple[datetime, ...]
) -> set[tuple[datetime, str, str]]:
    """Enumerate all not-yet-started player-games at every planner refresh."""

    starts = {game.game_id: game.start_time for game in team_week.games}
    return {
        (cutoff, player_game.sleeper_id, player_game.game_id)
        for cutoff in cutoffs
        for player_game in team_week.player_games
        if starts[player_game.game_id] > cutoff
    }


def _source_fingerprints(
    nba_inputs: HistoricalExperimentInputs,
) -> tuple[SourceFingerprint, ...]:
    """Bind every raw NBA artifact consumed by the projection builder."""

    return tuple(
        SourceFingerprint(
            f"nba:{artifact.season}:{artifact.resource}",
            artifact.sha256,
            "historical-experiment-input-v1",
        )
        for artifact in sorted(
            nba_inputs.artifacts,
            key=lambda item: (item.season, item.resource, item.sha256),
        )
    )


def _player_box_source_version(nba_inputs: HistoricalExperimentInputs) -> str:
    """Return a stable identity for all player-box history supplied to the baseline."""

    hashes = tuple(
        artifact.sha256
        for artifact in nba_inputs.artifacts
        if artifact.resource == "player_box_scores"
    )
    return sha256_text(canonical_json(hashes)) if hashes else "unversioned-player-box-source"


def _format_keys(keys: set[tuple[datetime, str, str]]) -> str:
    """Render deterministic missing-key evidence without dropping identities."""

    return ",".join(f"{at.isoformat()}:{player}:{game}" for at, player, game in sorted(keys))


__all__ = (
    "FULL_ADVISOR_CUTOFF_SCHEDULE_VERSION",
    "HistoricalProjectionSurface",
    "HistoricalProjectionSurfaceError",
    "PROJECTION_SURFACE_SCHEMA_VERSION",
    "ProjectionSurfaceEntry",
    "build_historical_projection_surface",
    "full_advisor_events",
    "full_advisor_planning_cutoffs",
    "player_games_with_projection_surface",
    "team_week_fingerprint",
    "validate_historical_projection_surface",
)
