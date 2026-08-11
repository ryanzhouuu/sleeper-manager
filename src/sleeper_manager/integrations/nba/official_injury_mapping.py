from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time

from sleeper_manager.domain.nba import AvailabilityStatus, ProviderPlayer, SourceMetadata
from sleeper_manager.integrations.nba.identity import MappingConfidence, MappingMethod
from sleeper_manager.integrations.nba.mapping import (
    normalize_player_name,
    normalize_report_player_name,
    normalize_team,
)
from sleeper_manager.integrations.nba.official_injury_report import (
    OfficialInjuryReportEntry,
    OfficialInjuryReportSnapshot,
)


@dataclass(frozen=True, slots=True)
class HistoricalPlayerAvailability:
    player_id: str
    game_date: date
    game_time: time
    matchup: str
    team_abbreviation: str
    status: AvailabilityStatus
    detail: str | None
    available_as_of: datetime
    source: SourceMetadata


@dataclass(frozen=True, slots=True)
class UnresolvedInjuryIdentity:
    entry: OfficialInjuryReportEntry
    reason: str
    candidate_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OfficialInjuryMapping:
    entry: OfficialInjuryReportEntry
    player_id: str | None
    method: MappingMethod
    confidence: MappingConfidence
    reason: str
    candidate_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OfficialInjuryMappingReport:
    mappings: tuple[OfficialInjuryMapping, ...]
    availability: tuple[HistoricalPlayerAvailability, ...]
    unresolved: tuple[UnresolvedInjuryIdentity, ...]
    warnings: tuple[str, ...] = ()


def map_official_injury_report(
    snapshot: OfficialInjuryReportSnapshot,
    provider_players: Iterable[ProviderPlayer],
) -> OfficialInjuryMappingReport:
    candidates = tuple(provider_players)
    by_name_team: dict[tuple[str, str], list[ProviderPlayer]] = {}
    for player in candidates:
        team = normalize_team(player.team_abbreviation)
        if team is not None:
            name_key = normalize_player_name(player.full_name)
            by_name_team.setdefault((name_key, team), []).append(player)

    mappings: list[OfficialInjuryMapping] = []
    availability: list[HistoricalPlayerAvailability] = []
    unresolved: list[UnresolvedInjuryIdentity] = []
    warnings: list[str] = []
    for entry in snapshot.entries:
        team = normalize_team(entry.team_abbreviation)
        matches = (
            by_name_team.get((normalize_report_player_name(entry.player_name), team), [])
            if team is not None
            else []
        )
        if len(matches) == 1:
            player = matches[0]
            mapping = OfficialInjuryMapping(
                entry=entry,
                player_id=player.provider_id,
                method=MappingMethod.NORMALIZED_NAME_TEAM,
                confidence=MappingConfidence.MEDIUM,
                reason="Matched by normalized official report name and team",
            )
            mappings.append(mapping)
            availability.append(
                HistoricalPlayerAvailability(
                    player_id=player.provider_id,
                    game_date=entry.game_date,
                    game_time=entry.game_time,
                    matchup=entry.matchup,
                    team_abbreviation=entry.team_abbreviation,
                    status=entry.status,
                    detail=entry.reason,
                    available_as_of=snapshot.published_at,
                    source=snapshot.source,
                )
            )
            continue

        candidate_ids = tuple(player.provider_id for player in matches)
        reason = (
            "Multiple provider players matched normalized report name and team"
            if len(matches) > 1
            else "No provider player matched normalized report name and team"
        )
        mappings.append(
            OfficialInjuryMapping(
                entry=entry,
                player_id=None,
                method=MappingMethod.UNRESOLVED,
                confidence=MappingConfidence.NONE,
                reason=reason,
                candidate_ids=candidate_ids,
            )
        )
        unresolved.append(
            UnresolvedInjuryIdentity(
                entry=entry,
                reason=reason,
                candidate_ids=candidate_ids,
            )
        )
        warnings.append(
            f"Unresolved official injury identity: {entry.player_name} ({entry.team_abbreviation})"
        )

    return OfficialInjuryMappingReport(
        mappings=tuple(mappings),
        availability=tuple(availability),
        unresolved=tuple(unresolved),
        warnings=tuple(warnings),
    )
