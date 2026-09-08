"""Build identity mappings and point-in-time projections for one team-week.

This module converts cached NBA records into selected-player replay evidence.
Raw archive access and provenance fingerprints remain in ``team_week_sources``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

from sleeper_manager.backtesting.experiments.data import (
    HistoricalExperimentInputs,
    dataset_version_for,
)
from sleeper_manager.backtesting.replay.inputs.models import PlayerTeamObservation
from sleeper_manager.backtesting.replay.league_archive import HistoricalLeagueArchive
from sleeper_manager.backtesting.replay.roster_timeline import EASTERN_TIME, FantasyWeekBoundary
from sleeper_manager.backtesting.replay.team_week_sources import HistoricalTeamWeekBundleError
from sleeper_manager.domain.nba import GameStatus, PlayerBoxScore, ScheduledGame
from sleeper_manager.domain.projection import ProjectionSnapshot
from sleeper_manager.integrations.nba.historical_feature_models import FEATURE_SCHEMA_VERSION
from sleeper_manager.integrations.nba.identity import (
    MappingConfidence,
    MappingMethod,
    PlayerIdentityMapper,
    PlayerMapping,
    parse_sleeper_player_identity,
)
from sleeper_manager.projections.direct_baseline import (
    MISSING_WARMUP_REASON,
    DirectBaselineObservation,
    DirectFantasyPointBaseline,
    PregameProjectionRequest,
    ProjectionBaselineError,
)

APPROXIMATE_FINALIZATION_POLICY_VERSION = "approximate-next-eastern-day-0600-v1"


def player_mappings(
    roster_player_ids: Sequence[str],
    catalog: Mapping[str, Mapping[str, object]],
    nba_inputs: HistoricalExperimentInputs,
) -> tuple[PlayerMapping, ...]:
    """Resolve only players who overlap the selected roster-week to provider identities."""

    identities = []
    unresolved: list[PlayerMapping] = []
    for sleeper_id in roster_player_ids:
        payload = catalog.get(sleeper_id)
        if payload is None:
            unresolved.append(
                PlayerMapping(
                    sleeper_id,
                    None,
                    MappingMethod.UNRESOLVED,
                    MappingConfidence.NONE,
                    "Sleeper player catalog has no record for this rostered player.",
                )
            )
            continue
        try:
            identities.append(parse_sleeper_player_identity(sleeper_id, payload))
        except ValueError as error:
            unresolved.append(
                PlayerMapping(
                    sleeper_id,
                    None,
                    MappingMethod.UNRESOLVED,
                    MappingConfidence.NONE,
                    str(error),
                )
            )
    resolved = PlayerIdentityMapper().resolve(identities, nba_inputs.provider_players)
    return tuple(sorted((*resolved.mappings, *unresolved), key=lambda item: item.sleeper_id))


def projection_snapshots(
    nba_inputs: HistoricalExperimentInputs,
    mappings: Sequence[PlayerMapping],
    boundary: FantasyWeekBoundary,
    archive: HistoricalLeagueArchive,
    baseline: DirectFantasyPointBaseline,
    *,
    targets: Sequence[PlayerBoxScore] | None = None,
) -> tuple[ProjectionSnapshot, ...]:
    """Project targets using only finalized history from the original NBA input cache.

    Supplemental targets select requests; their outcomes never enter training history.
    """

    games = {game.provider_id: game for game in nba_inputs.games}
    mapping_by_provider = {
        mapping.espn_id: mapping for mapping in mappings if mapping.espn_id is not None
    }
    source_version = _player_box_source_version(nba_inputs)
    observations = tuple(
        _direct_observation(box_score, games, source_version)
        for box_score in nba_inputs.player_box_scores
        if box_score.game_id in games
    )
    dataset_version = dataset_version_for(
        nba_inputs.artifacts,
        scoring_policy=archive.scoring_policy,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
    )
    snapshots: list[ProjectionSnapshot] = []
    for box_score in nba_inputs.player_box_scores if targets is None else targets:
        game = games.get(box_score.game_id)
        mapping = mapping_by_provider.get(box_score.player_id)
        if game is None or mapping is None:
            continue
        if not boundary.utc_start <= game.start_time < boundary.utc_end:
            continue
        request = PregameProjectionRequest(
            dataset_version=dataset_version,
            feature_schema_version=FEATURE_SCHEMA_VERSION,
            player_id=mapping.sleeper_id,
            history_player_id=box_score.player_id,
            game_id=game.provider_id,
            game_start=game.start_time,
            available_as_of=game.start_time - timedelta(minutes=30),
            history=observations,
        )
        try:
            snapshots.append(
                baseline.project_pregame(request, scoring_policy=archive.scoring_policy)
            )
        except ProjectionBaselineError as error:
            if error.reason_code != MISSING_WARMUP_REASON:
                raise HistoricalTeamWeekBundleError(str(error)) from error
    return tuple(sorted(snapshots, key=lambda item: (item.game_id, item.player_id)))


def selected_games_and_box_scores(
    nba_inputs: HistoricalExperimentInputs,
    mappings: Sequence[PlayerMapping],
    boundary: FantasyWeekBoundary,
) -> tuple[tuple[ScheduledGame, ...], tuple[PlayerBoxScore, ...]]:
    """Keep the entire week's schedule even when mapped player outcomes are absent."""

    games_by_id = {game.provider_id: game for game in nba_inputs.games}
    mapped_provider_ids = {mapping.espn_id for mapping in mappings if mapping.espn_id is not None}
    box_scores = tuple(
        box_score
        for box_score in nba_inputs.player_box_scores
        if box_score.player_id in mapped_provider_ids
        and (game := games_by_id.get(box_score.game_id)) is not None
        and boundary.utc_start <= game.start_time < boundary.utc_end
    )
    games = tuple(
        replace(
            game,
            finalized_at=game.finalized_at or approximate_finalization_at(game.start_time),
        )
        if game.status is GameStatus.FINAL
        else game
        for game in nba_inputs.games
        if boundary.utc_start <= game.start_time < boundary.utc_end
    )
    return games, box_scores


def team_observations(
    nba_inputs: HistoricalExperimentInputs, mappings: Sequence[PlayerMapping]
) -> tuple[PlayerTeamObservation, ...]:
    """Freeze season-wide team associations separately from selected-week outcome rows."""
    games = {game.provider_id: game for game in nba_inputs.games}
    providers = {mapping.espn_id for mapping in mappings if mapping.espn_id is not None}
    observations = {
        PlayerTeamObservation(
            box.player_id,
            box.team_id,
            games[box.game_id].start_time,
            f"{box.source.provider}:player-box:{box.game_id}:{box.player_id}",
        )
        for box in nba_inputs.player_box_scores
        if box.player_id in providers and box.game_id in games
    }
    return tuple(
        sorted(
            observations,
            key=lambda item: (item.provider_player_id, item.observed_at, item.team_id, item.source),
        )
    )


def approximate_finalization_at(start: datetime) -> datetime:
    """Return the next local day's 6 AM Eastern bound when raw data lacks a final timestamp."""

    local_next_day = start.astimezone(EASTERN_TIME).date() + timedelta(days=1)
    return datetime.combine(local_next_day, time(6), tzinfo=EASTERN_TIME).astimezone(UTC)


def _direct_observation(
    box_score: PlayerBoxScore,
    games: Mapping[str, ScheduledGame],
    source_version: str,
) -> DirectBaselineObservation:
    """Expose a box score no earlier than its approximate, next-day 6 AM finalization bound."""

    game = games[box_score.game_id]
    return DirectBaselineObservation(
        player_id=box_score.player_id,
        game_id=box_score.game_id,
        game_start=game.start_time,
        outcome_finalized_at=approximate_finalization_at(game.start_time),
        minutes=box_score.minutes,
        started=box_score.started,
        did_not_play=not box_score.did_play,
        box_score=box_score.line,
        source_version=source_version,
    )


def _player_box_source_version(nba_inputs: HistoricalExperimentInputs) -> str:
    """Return the content hash that backs all direct-baseline player observations."""

    artifact = next(
        (item for item in nba_inputs.artifacts if item.resource == "player_box_scores"),
        None,
    )
    return artifact.sha256 if artifact is not None else "unversioned-player-box-source"


__all__ = (
    "APPROXIMATE_FINALIZATION_POLICY_VERSION",
    "approximate_finalization_at",
    "player_mappings",
    "projection_snapshots",
    "selected_games_and_box_scores",
    "team_observations",
)
