"""Collect live provider evidence and package it into planning inputs."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from sleeper_manager.domain.league import LeagueProfile
from sleeper_manager.domain.models import Roster
from sleeper_manager.domain.nba import (
    DataQualityReport,
    DataQualityState,
    PlayerAvailability,
    ProviderPlayer,
    ProviderResult,
    ScheduledGame,
)
from sleeper_manager.domain.planning import (
    AcknowledgedDecisionEvidence,
    PlanningReasonCode,
)
from sleeper_manager.integrations.nba.base import NBAProvider
from sleeper_manager.integrations.nba.identity import (
    PlayerIdentityMapper,
    SleeperPlayerIdentity,
    parse_sleeper_player_identity,
)
from sleeper_manager.projections.live_baseline import LiveProjectionTarget
from sleeper_manager.workflows.planning_inputs import (
    AvailabilityResourceResult,
    FantasyWeekWindow,
    LivePlanningInputs,
    LiveProjectionProvider,
    LiveProjectionResult,
    PlanningFreshnessPolicy,
    PlanningInputsError,
    PlayerEligibilityEvidence,
    ResolvedPlayerIdentity,
    ScheduleResourceResult,
)


class PlanningCollectionError(RuntimeError):
    pass


class LeagueProfileSource(Protocol):
    async def fetch(self) -> LeagueProfile: ...


class SleeperPlayerCatalogSource(Protocol):
    async def players(self) -> Mapping[str, Mapping[str, Any]]: ...


class AcknowledgementSource(Protocol):
    """Supplies durable manager decisions; the real repository query lands in Task 5.2."""

    async def load(
        self, league_id: str, week: int, *, as_of: datetime
    ) -> tuple[AcknowledgedDecisionEvidence, ...]: ...


@dataclass(frozen=True, slots=True)
class CollectedLiveEvidence:
    inputs: LivePlanningInputs
    decision_time: datetime
    warnings: tuple[str, ...]


async def collect_live_planning_inputs(
    *,
    profile_source: LeagueProfileSource,
    catalog_source: SleeperPlayerCatalogSource,
    nba: NBAProvider,
    projection_provider: LiveProjectionProvider,
    week_window: FantasyWeekWindow,
    freshness_policy: PlanningFreshnessPolicy,
    acknowledgement_source: AcknowledgementSource | None = None,
    mapping_overrides: Mapping[str, str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> CollectedLiveEvidence:
    """Fetch current evidence read-only; assembly stays pure and offline.

    The planning instant is captured after every provider call so collected
    evidence is never newer than the returned decision time.
    """
    raw_tick = clock or (lambda: datetime.now(UTC))

    def tick() -> datetime:
        moment = raw_tick()
        if moment.tzinfo is None:
            raise PlanningCollectionError(
                "The collection clock must produce timezone-aware timestamps"
            )
        return moment

    profile = await profile_source.fetch()
    roster = _manager_roster(profile)
    catalog = await catalog_source.players()
    catalog_retrieved_at = tick()
    warnings: list[str] = []

    identities_raw, catalog_warnings = _sleeper_identities(roster, catalog)
    warnings.extend(catalog_warnings)

    teams = sorted({identity.team for identity in identities_raw if identity.team})
    provider_players, identity_reports, roster_warnings = await _team_rosters(nba, teams, tick)
    warnings.extend(roster_warnings)

    mapping = PlayerIdentityMapper().resolve(
        identities_raw,
        provider_players,
        overrides=dict(mapping_overrides) if mapping_overrides else None,
    )
    mapping_by_sleeper = {item.sleeper_id: item for item in mapping.mappings}
    player_by_provider = {player.provider_id: player for player in provider_players}
    identities = tuple(
        _resolved_identity(player_id, mapping_by_sleeper, player_by_provider)
        for player_id in roster.player_ids
    )
    eligibility = tuple(
        _eligibility_evidence(player_id, catalog, catalog_retrieved_at)
        for player_id in roster.player_ids
    )

    schedule_results, schedule_warnings = await _team_schedules(nba, profile, identities, tick)
    warnings.extend(schedule_warnings)
    availability_result, availability_warnings = await _injury_availability(nba, tick)
    warnings.extend(availability_warnings)

    decision_time = tick()
    projections = _project_targets(
        profile=profile,
        identities=identities,
        schedule_results=schedule_results,
        projection_provider=projection_provider,
        week_window=week_window,
        decision_time=decision_time,
    )
    acknowledgements = (
        await acknowledgement_source.load(profile.league_id, week_window.week, as_of=decision_time)
        if acknowledgement_source is not None
        else ()
    )
    try:
        inputs = LivePlanningInputs(
            league_profile=profile,
            week_window=week_window,
            freshness_policy=freshness_policy,
            player_eligibility=eligibility,
            identities=identities,
            schedule_results=tuple(schedule_results),
            availability_results=(availability_result,),
            projections=tuple(projections),
            acknowledgements=acknowledgements,
            identity_quality_reports=tuple(identity_reports),
        )
    except PlanningInputsError as error:
        raise PlanningCollectionError(str(error)) from error
    return CollectedLiveEvidence(
        inputs=inputs, decision_time=decision_time, warnings=tuple(warnings)
    )


def _manager_roster(profile: LeagueProfile) -> Roster:
    roster = next(
        (item for item in profile.rosters if item.roster_id == profile.manager_roster_id),
        None,
    )
    if roster is None:
        raise PlanningCollectionError(
            f"Manager roster {profile.manager_roster_id} is missing from the league profile"
        )
    return roster


def _sleeper_identities(
    roster: Roster,
    catalog: Mapping[str, Mapping[str, Any]],
) -> tuple[list[SleeperPlayerIdentity], list[str]]:
    identities: list[SleeperPlayerIdentity] = []
    warnings: list[str] = []
    for player_id in roster.player_ids:
        raw_player = catalog.get(player_id)
        if raw_player is None:
            warnings.append(f"{player_id}: missing from the Sleeper player catalog")
            identities.append(_placeholder_identity(player_id))
            continue
        try:
            identities.append(parse_sleeper_player_identity(player_id, raw_player))
        except ValueError as error:
            warnings.append(f"{player_id}: {error}")
            identities.append(_placeholder_identity(player_id))
    return identities, warnings


def _placeholder_identity(player_id: str) -> SleeperPlayerIdentity:
    return SleeperPlayerIdentity(sleeper_id=player_id, full_name=player_id, team=None, espn_id=None)


async def _team_rosters(
    nba: NBAProvider,
    teams: list[str],
    tick: Callable[[], datetime],
) -> tuple[list[ProviderPlayer], list[DataQualityReport], list[str]]:
    provider_players: list[ProviderPlayer] = []
    reports: list[DataQualityReport] = []
    warnings: list[str] = []
    for team_id in teams:
        resource = f"nba:team-roster:{team_id}"
        try:
            result = await nba.team_roster(team_id)
        except Exception as error:
            warnings.append(f"{resource} failed: {error}")
            reports.append(_error_report(resource, tick()))
            continue
        reports.append(replace(result.quality, resource=resource))
        provider_players.extend(result.records)
    return provider_players, reports, warnings


def _resolved_identity(
    player_id: str,
    mapping_by_sleeper: Mapping[str, Any],
    player_by_provider: Mapping[str, ProviderPlayer],
) -> ResolvedPlayerIdentity:
    mapping = mapping_by_sleeper.get(player_id)
    if mapping is None or mapping.espn_id is None:
        reason = mapping.reason if mapping is not None else "no identity evidence"
        return ResolvedPlayerIdentity(
            sleeper_player_id=player_id,
            provider_player_id=None,
            provider_team_id=None,
            method="unresolved",
            confidence="none",
            reason=reason,
        )
    provider_team_id = None
    provider_player = player_by_provider.get(mapping.espn_id)
    if provider_player is not None:
        provider_team_id = provider_player.team_id
    return ResolvedPlayerIdentity(
        sleeper_player_id=player_id,
        provider_player_id=mapping.espn_id,
        provider_team_id=provider_team_id,
        method=mapping.method.value,
        confidence=mapping.confidence.value,
        reason=mapping.reason,
    )


def _eligibility_evidence(
    player_id: str,
    catalog: Mapping[str, Mapping[str, Any]],
    retrieved_at: datetime,
) -> PlayerEligibilityEvidence:
    raw_player = catalog.get(player_id)
    positions: tuple[str, ...] = ()
    if raw_player is not None:
        positions = tuple(
            position for position in raw_player.get("fantasy_positions", []) if position
        )
    return PlayerEligibilityEvidence(
        sleeper_player_id=player_id,
        eligible_positions=positions,
        available_as_of=retrieved_at,
        provenance="sleeper-player-catalog",
    )


async def _team_schedules(
    nba: NBAProvider,
    profile: LeagueProfile,
    identities: tuple[ResolvedPlayerIdentity, ...],
    tick: Callable[[], datetime],
) -> tuple[list[ScheduleResourceResult], list[str]]:
    team_ids = sorted({item.provider_team_id for item in identities if item.provider_team_id})
    season = _season_year(profile)
    results: list[ScheduleResourceResult] = []
    warnings: list[str] = []
    for team_id in team_ids:
        resource = f"nba:team-schedule:{team_id}"
        try:
            result: ProviderResult[tuple[ScheduledGame, ...]] = await nba.team_schedule(
                team_id, season
            )
        except Exception as error:
            warnings.append(f"{resource} failed: {error}")
            results.append(
                ScheduleResourceResult(
                    resource=resource,
                    games=(),
                    quality=_error_report(resource, tick()),
                )
            )
            continue
        results.append(
            ScheduleResourceResult(resource=resource, games=result.records, quality=result.quality)
        )
    return results, warnings


async def _injury_availability(
    nba: NBAProvider,
    tick: Callable[[], datetime],
) -> tuple[AvailabilityResourceResult, list[str]]:
    resource = "nba:injuries"
    try:
        result: ProviderResult[tuple[PlayerAvailability, ...]] = await nba.injuries()
    except Exception as error:
        return (
            AvailabilityResourceResult(
                resource=resource,
                records=(),
                quality=_error_report(resource, tick()),
            ),
            [f"{resource} failed: {error}"],
        )
    return (
        AvailabilityResourceResult(
            resource=resource, records=result.records, quality=result.quality
        ),
        [],
    )


def _project_targets(
    *,
    profile: LeagueProfile,
    identities: tuple[ResolvedPlayerIdentity, ...],
    schedule_results: list[ScheduleResourceResult],
    projection_provider: LiveProjectionProvider,
    week_window: FantasyWeekWindow,
    decision_time: datetime,
) -> list[LiveProjectionResult]:
    games_by_team: dict[str, dict[str, datetime]] = defaultdict(dict)
    for result in schedule_results:
        if result.quality.state is DataQualityState.ERROR:
            continue
        for game in result.games:
            if not week_window.contains(game.start_time):
                continue
            for team_id in (game.home_team_id, game.away_team_id):
                games_by_team[team_id][game.provider_id] = game.start_time

    projections: list[LiveProjectionResult] = []
    seen: set[tuple[str, str]] = set()
    for identity in identities:
        if identity.provider_player_id is None or identity.provider_team_id is None:
            continue
        for game_id, game_start in sorted(games_by_team.get(identity.provider_team_id, {}).items()):
            key = identity.sleeper_player_id, game_id
            if key in seen:
                continue
            seen.add(key)
            snapshot = projection_provider.project(
                LiveProjectionTarget(
                    sleeper_player_id=identity.sleeper_player_id,
                    game_id=game_id,
                    game_start=game_start,
                    provider_player_id=identity.provider_player_id,
                ),
                scoring_policy=profile.scoring,
                decision_time=decision_time,
            )
            projections.append(
                LiveProjectionResult(
                    sleeper_player_id=identity.sleeper_player_id,
                    game_id=game_id,
                    snapshot=snapshot,
                    missing_reason=(
                        None if snapshot is not None else PlanningReasonCode.MISSING_PROJECTION
                    ),
                )
            )
    return projections


def _season_year(profile: LeagueProfile) -> int:
    try:
        return int(profile.season)
    except ValueError as error:
        raise PlanningCollectionError(
            f"League season {profile.season!r} is not a valid year"
        ) from error


def _error_report(resource: str, retrieved_at: datetime) -> DataQualityReport:
    return DataQualityReport(
        state=DataQualityState.ERROR,
        resource=resource,
        record_count=0,
        retrieved_at=retrieved_at,
        source_updated_at=None,
        expires_at=None,
        errors=(f"{resource} could not be collected",),
    )


__all__ = (
    "AcknowledgementSource",
    "CollectedLiveEvidence",
    "LeagueProfileSource",
    "PlanningCollectionError",
    "SleeperPlayerCatalogSource",
    "collect_live_planning_inputs",
)
