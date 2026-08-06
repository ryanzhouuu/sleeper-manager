from dataclasses import dataclass
from datetime import date

from sleeper_manager.domain.nba import DataQualityReport, DataQualityState, ProviderPlayer
from sleeper_manager.integrations.nba.base import NBAProvider
from sleeper_manager.integrations.nba.identity import (
    MappingReport,
    PlayerIdentityMapper,
    SleeperPlayerIdentity,
    parse_sleeper_player_identity,
)
from sleeper_manager.integrations.sleeper.client import SleeperClient
from sleeper_manager.integrations.sleeper.sync import LeagueSynchronizationService


@dataclass(frozen=True, slots=True)
class NBAHealthReport:
    provider: str
    quality_reports: tuple[DataQualityReport, ...]
    mapping: MappingReport
    errors: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        acceptable_states = {DataQualityState.FRESH, DataQualityState.EMPTY}
        return (
            not self.errors
            and not self.mapping.unresolved
            and all(report.state in acceptable_states for report in self.quality_reports)
        )


def _missing_identity(player_id: str, reason: str) -> tuple[SleeperPlayerIdentity, str]:
    return SleeperPlayerIdentity(
        sleeper_id=player_id,
        full_name=player_id,
        team=None,
        espn_id=None,
    ), reason


async def collect_nba_diagnostics(
    sleeper: SleeperClient,
    nba: NBAProvider,
    *,
    league_id: str,
    user_id: str,
    game_date: date,
    mapping_overrides: dict[str, str] | None = None,
) -> NBAHealthReport:
    errors: list[str] = []
    quality_reports: list[DataQualityReport] = []
    try:
        sync_result = await LeagueSynchronizationService(sleeper).sync(
            league_id=league_id,
            user_id=user_id,
        )
        profile = sync_result.profile
    except Exception as error:
        return NBAHealthReport(
            provider="espn",
            quality_reports=(),
            mapping=MappingReport((), ()),
            errors=(f"Sleeper bootstrap failed: {error}",),
        )

    manager_roster = next(
        roster for roster in profile.rosters if roster.roster_id == profile.manager_roster_id
    )
    try:
        catalog = await sleeper.players(active=True)
    except Exception as error:
        catalog = {}
        errors.append(f"Sleeper player catalog failed: {error}")

    identities: list[SleeperPlayerIdentity] = []
    identity_warnings: list[str] = []
    for player_id in manager_roster.player_ids:
        raw_player = catalog.get(player_id)
        if raw_player is None:
            identity, reason = _missing_identity(
                player_id, "Sleeper player is missing from the catalog"
            )
            identities.append(identity)
            identity_warnings.append(f"{player_id}: {reason}")
            continue
        try:
            identities.append(parse_sleeper_player_identity(player_id, raw_player))
        except ValueError as error:
            identity, reason = _missing_identity(player_id, str(error))
            identities.append(identity)
            identity_warnings.append(f"{player_id}: {reason}")

    provider_players: list[ProviderPlayer] = []
    teams = sorted({identity.team for identity in identities if identity.team})
    for team_id in teams:
        try:
            roster_result = await nba.team_roster(team_id)
        except Exception as error:
            errors.append(f"ESPN team roster {team_id} failed: {error}")
            continue
        quality_reports.append(roster_result.quality)
        provider_players.extend(roster_result.records)

    try:
        scoreboard_result = await nba.scoreboard(game_date)
        quality_reports.append(scoreboard_result.quality)
    except Exception as error:
        errors.append(f"ESPN scoreboard failed: {error}")

    mapping = PlayerIdentityMapper().resolve(
        identities,
        provider_players,
        overrides=mapping_overrides,
    )
    if identity_warnings:
        mapping = MappingReport(
            mappings=mapping.mappings,
            warnings=mapping.warnings + tuple(identity_warnings),
        )
    return NBAHealthReport(
        provider="espn",
        quality_reports=tuple(quality_reports),
        mapping=mapping,
        errors=tuple(errors),
    )
