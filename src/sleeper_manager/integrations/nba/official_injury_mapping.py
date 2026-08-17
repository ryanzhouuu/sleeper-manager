from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum

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


class InjuryMappingCategory(StrEnum):
    RESOLVED = "resolved"
    RESOLVED_NAME_ONLY = "resolved_name_only"
    RESOLVED_PARTIAL_NAME_TEAM = "resolved_partial_name_team"
    RESOLVED_SUBSET_NAME_TEAM = "resolved_subset_name_team"
    RESOLVED_HISTORICAL_NAME_TEAM = "resolved_historical_name_team"
    NO_NAME_TEAM_MATCH = "no_name_team_match"
    AMBIGUOUS_NAME_TEAM_MATCH = "ambiguous_name_team_match"
    AMBIGUOUS_NAME_ONLY = "ambiguous_name_only"
    AMBIGUOUS_PARTIAL_NAME_TEAM = "ambiguous_partial_name_team"
    AMBIGUOUS_SUBSET_NAME_TEAM = "ambiguous_subset_name_team"


@dataclass(frozen=True, slots=True)
class InjuryMappingDiagnostic:
    category: InjuryMappingCategory
    season: int
    team_abbreviation: str
    normalized_name: str
    count: int


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
    diagnostics: tuple[InjuryMappingDiagnostic, ...] = ()


def map_official_injury_report(
    snapshot: OfficialInjuryReportSnapshot,
    provider_players: Iterable[ProviderPlayer],
    *,
    historical_player_ids_by_date_team: Mapping[tuple[date, str], frozenset[str]] | None = None,
) -> OfficialInjuryMappingReport:
    candidates = tuple(provider_players)
    by_name_team: dict[tuple[str, str], list[ProviderPlayer]] = {}
    by_name_ids: dict[str, set[str]] = {}
    by_first_name_team: dict[tuple[str, str], set[str]] = {}
    by_token_name_team: dict[str, list[tuple[str, frozenset[str], tuple[str, ...]]]] = {}
    for player in candidates:
        name_key = normalize_player_name(player.full_name)
        by_name_ids.setdefault(name_key, set()).add(player.provider_id)
        team = normalize_team(player.team_abbreviation)
        if team is not None:
            by_name_team.setdefault((name_key, team), []).append(player)
            first_name = name_key.split(maxsplit=1)[0] if name_key else ""
            if first_name:
                by_first_name_team.setdefault((first_name, team), set()).add(player.provider_id)
            name_tokens = _name_tokens(player.full_name)
            if name_tokens:
                by_token_name_team.setdefault(team, []).append(
                    (player.provider_id, frozenset(name_tokens), name_tokens)
                )

    mappings: list[OfficialInjuryMapping] = []
    availability: list[HistoricalPlayerAvailability] = []
    unresolved: list[UnresolvedInjuryIdentity] = []
    warnings: list[str] = []
    diagnostics: Counter[tuple[InjuryMappingCategory, int, str, str]] = Counter()
    for entry in snapshot.entries:
        team = normalize_team(entry.team_abbreviation)
        normalized_name = normalize_report_player_name(entry.player_name)
        matches = by_name_team.get((normalized_name, team), []) if team is not None else []
        team_match_ids = tuple(sorted({player.provider_id for player in matches}))
        if len(team_match_ids) == 1:
            player_id = team_match_ids[0]
            mapping = OfficialInjuryMapping(
                entry=entry,
                player_id=player_id,
                method=MappingMethod.NORMALIZED_NAME_TEAM,
                confidence=MappingConfidence.MEDIUM,
                reason="Matched by normalized official report name and team",
            )
            mappings.append(mapping)
            diagnostics[
                (
                    InjuryMappingCategory.RESOLVED,
                    _season_start_year(entry.game_date),
                    team or entry.team_abbreviation.casefold(),
                    normalized_name,
                )
            ] += 1
            availability.append(
                HistoricalPlayerAvailability(
                    player_id=player_id,
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

        partial_ids = (
            tuple(sorted(by_first_name_team.get((normalized_name, team), set())))
            if team is not None and len(normalized_name.split()) == 1
            else ()
        )
        historical_partial_ids = (
            tuple(
                player_id
                for player_id in partial_ids
                if player_id
                in historical_player_ids_by_date_team.get((entry.game_date, team), frozenset())
            )
            if historical_player_ids_by_date_team is not None and team is not None
            else ()
        )
        if len(partial_ids) > 1 and len(historical_partial_ids) == 1:
            player_id = historical_partial_ids[0]
            mappings.append(
                OfficialInjuryMapping(
                    entry=entry,
                    player_id=player_id,
                    method=MappingMethod.NORMALIZED_HISTORICAL_NAME_TEAM,
                    confidence=MappingConfidence.MEDIUM,
                    reason=(
                        "Matched by unique normalized first name and official team; "
                        "historical box-score evidence identified one candidate on the game date"
                    ),
                    candidate_ids=historical_partial_ids,
                )
            )
            diagnostics[
                (
                    InjuryMappingCategory.RESOLVED_HISTORICAL_NAME_TEAM,
                    _season_start_year(entry.game_date),
                    team or entry.team_abbreviation.casefold(),
                    normalized_name,
                )
            ] += 1
            availability.append(
                HistoricalPlayerAvailability(
                    player_id=player_id,
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
        if len(partial_ids) == 1:
            player_id = partial_ids[0]
            mappings.append(
                OfficialInjuryMapping(
                    entry=entry,
                    player_id=player_id,
                    method=MappingMethod.NORMALIZED_PARTIAL_NAME_TEAM,
                    confidence=MappingConfidence.LOW,
                    reason=(
                        "Matched by unique normalized first name and official team; "
                        "full report name was incomplete"
                    ),
                    candidate_ids=partial_ids,
                )
            )
            diagnostics[
                (
                    InjuryMappingCategory.RESOLVED_PARTIAL_NAME_TEAM,
                    _season_start_year(entry.game_date),
                    team or entry.team_abbreviation.casefold(),
                    normalized_name,
                )
            ] += 1
            availability.append(
                HistoricalPlayerAvailability(
                    player_id=player_id,
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

        subset_ids = _subset_matches(
            normalized_name,
            team,
            by_token_name_team.get(team or "", ()),
        )
        if len(subset_ids) == 1:
            player_id = subset_ids[0]
            mappings.append(
                OfficialInjuryMapping(
                    entry=entry,
                    player_id=player_id,
                    method=MappingMethod.NORMALIZED_SUBSET_NAME_TEAM,
                    confidence=MappingConfidence.LOW,
                    reason=(
                        "Matched by unique normalized report-name token subset and official team"
                    ),
                    candidate_ids=subset_ids,
                )
            )
            diagnostics[
                (
                    InjuryMappingCategory.RESOLVED_SUBSET_NAME_TEAM,
                    _season_start_year(entry.game_date),
                    team or entry.team_abbreviation.casefold(),
                    normalized_name,
                )
            ] += 1
            availability.append(
                HistoricalPlayerAvailability(
                    player_id=player_id,
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

        name_only_ids = tuple(sorted(by_name_ids.get(normalized_name, set())))
        if len(name_only_ids) == 1 and not partial_ids:
            player_id = name_only_ids[0]
            mappings.append(
                OfficialInjuryMapping(
                    entry=entry,
                    player_id=player_id,
                    method=MappingMethod.NORMALIZED_NAME_ONLY,
                    confidence=MappingConfidence.LOW,
                    reason=(
                        "Matched by unique normalized report name; provider team "
                        "confirmation was unavailable"
                    ),
                    candidate_ids=name_only_ids,
                )
            )
            diagnostics[
                (
                    InjuryMappingCategory.RESOLVED_NAME_ONLY,
                    _season_start_year(entry.game_date),
                    team or entry.team_abbreviation.casefold(),
                    normalized_name,
                )
            ] += 1
            availability.append(
                HistoricalPlayerAvailability(
                    player_id=player_id,
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

        candidate_ids = team_match_ids or partial_ids or subset_ids or name_only_ids
        category = (
            InjuryMappingCategory.AMBIGUOUS_NAME_TEAM_MATCH
            if team_match_ids
            else (
                InjuryMappingCategory.AMBIGUOUS_PARTIAL_NAME_TEAM
                if partial_ids
                else (
                    InjuryMappingCategory.AMBIGUOUS_SUBSET_NAME_TEAM
                    if subset_ids
                    else (
                        InjuryMappingCategory.AMBIGUOUS_NAME_ONLY
                        if name_only_ids
                        else InjuryMappingCategory.NO_NAME_TEAM_MATCH
                    )
                )
            )
        )
        diagnostics[
            (
                category,
                _season_start_year(entry.game_date),
                team or entry.team_abbreviation.casefold(),
                normalized_name,
            )
        ] += 1
        reason = (
            "Multiple provider players matched normalized report name and team"
            if team_match_ids
            else (
                "Multiple provider players matched normalized first name and official team"
                if partial_ids
                else (
                    "Multiple provider players matched normalized report-name tokens and "
                    "official team"
                    if subset_ids
                    else (
                        "Multiple provider players matched normalized report name without "
                        "team confirmation"
                        if name_only_ids
                        else "No provider player matched normalized report name and team"
                    )
                )
            )
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
        diagnostics=tuple(
            InjuryMappingDiagnostic(
                category=category,
                season=season,
                team_abbreviation=team_abbreviation,
                normalized_name=normalized_name,
                count=count,
            )
            for (category, season, team_abbreviation, normalized_name), count in sorted(
                diagnostics.items(),
                key=lambda item: item[0],
            )
        ),
    )


def _season_start_year(value: date) -> int:
    return value.year if value.month >= 10 else value.year - 1


def _name_tokens(name: str) -> tuple[str, ...]:
    tokens = normalize_player_name(name).split()
    merged: list[str] = []
    for token in tokens:
        if len(token) == 1 and merged and len(merged[-1]) == 1:
            merged[-1] += token
        else:
            merged.append(token)
    return tuple(merged)


def _subset_matches(
    normalized_report_name: str,
    team: str | None,
    candidates: Iterable[tuple[str, frozenset[str], tuple[str, ...]]],
) -> tuple[str, ...]:
    if team is None:
        return ()
    report_tokens = frozenset(_name_tokens(normalized_report_name))
    if len(report_tokens) < 2:
        return ()
    matches = {
        provider_id
        for provider_id, provider_tokens, provider_token_sequence in candidates
        if report_tokens <= provider_tokens
        and (
            report_tokens < provider_tokens
            or provider_token_sequence != tuple(normalized_report_name.split())
        )
    }
    return tuple(sorted(matches))
