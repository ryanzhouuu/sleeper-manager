from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from sleeper_manager.domain.nba import ProviderPlayer
from sleeper_manager.integrations.nba.mapping import normalize_player_name, normalize_team
from sleeper_manager.integrations.sleeper.schemas import SleeperPlayerPayload


class MappingMethod(StrEnum):
    EXPLICIT_OVERRIDE = "explicit_override"
    STABLE_ID = "stable_id"
    NORMALIZED_NAME_TEAM = "normalized_name_team"
    NORMALIZED_NAME_ONLY = "normalized_name_only"
    UNRESOLVED = "unresolved"


class MappingConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class SleeperPlayerIdentity:
    sleeper_id: str
    full_name: str
    team: str | None
    espn_id: str | None = None


@dataclass(frozen=True, slots=True)
class PlayerMapping:
    sleeper_id: str
    espn_id: str | None
    method: MappingMethod
    confidence: MappingConfidence
    reason: str
    candidate_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MappingReport:
    mappings: tuple[PlayerMapping, ...]
    warnings: tuple[str, ...] = ()

    @property
    def resolved(self) -> tuple[PlayerMapping, ...]:
        return tuple(mapping for mapping in self.mappings if mapping.espn_id is not None)

    @property
    def unresolved(self) -> tuple[PlayerMapping, ...]:
        return tuple(mapping for mapping in self.mappings if mapping.espn_id is None)


def parse_sleeper_player_identity(
    sleeper_id: str, payload: Mapping[str, Any]
) -> SleeperPlayerIdentity:
    try:
        player = SleeperPlayerPayload.model_validate(payload)
    except ValidationError as error:
        raise ValueError(f"Invalid Sleeper player payload for {sleeper_id!r}: {error}") from error
    full_name = player.full_name or " ".join(
        part for part in (player.first_name, player.last_name) if part
    )
    if not full_name.strip():
        raise ValueError(f"Sleeper player {sleeper_id!r} has no name")
    return SleeperPlayerIdentity(
        sleeper_id=sleeper_id,
        full_name=full_name.strip(),
        team=player.team,
        espn_id=str(player.espn_id) if player.espn_id is not None else None,
    )


class PlayerIdentityMapper:
    """Resolve Sleeper player identities without fuzzy or ambiguous matches."""

    def resolve(
        self,
        sleeper_players: Iterable[SleeperPlayerIdentity],
        provider_players: Iterable[ProviderPlayer],
        *,
        overrides: Mapping[str, str] | None = None,
    ) -> MappingReport:
        candidates = tuple(provider_players)
        by_id = {player.provider_id: player for player in candidates}
        by_name: dict[str, list[ProviderPlayer]] = {}
        by_name_team: dict[tuple[str, str], list[ProviderPlayer]] = {}
        for player in candidates:
            name_key = normalize_player_name(player.full_name)
            by_name.setdefault(name_key, []).append(player)
            team_key = normalize_team(player.team_abbreviation)
            if team_key is not None:
                by_name_team.setdefault((name_key, team_key), []).append(player)

        mapping_results: list[PlayerMapping] = []
        warnings: list[str] = []
        configured_overrides = overrides or {}
        for sleeper_player in sleeper_players:
            override = configured_overrides.get(sleeper_player.sleeper_id)
            if override is not None:
                if override in by_id:
                    mapping_results.append(
                        PlayerMapping(
                            sleeper_id=sleeper_player.sleeper_id,
                            espn_id=override,
                            method=MappingMethod.EXPLICIT_OVERRIDE,
                            confidence=MappingConfidence.HIGH,
                            reason="Matched by manager policy override",
                        )
                    )
                    continue
                mapping_results.append(
                    PlayerMapping(
                        sleeper_id=sleeper_player.sleeper_id,
                        espn_id=None,
                        method=MappingMethod.UNRESOLVED,
                        confidence=MappingConfidence.NONE,
                        reason=f"Configured ESPN override {override!r} was not found",
                        candidate_ids=(),
                    )
                )
                warnings.append(
                    f"Sleeper player {sleeper_player.sleeper_id} has an invalid ESPN override"
                )
                continue

            if sleeper_player.espn_id is not None and sleeper_player.espn_id in by_id:
                mapping_results.append(
                    PlayerMapping(
                        sleeper_id=sleeper_player.sleeper_id,
                        espn_id=sleeper_player.espn_id,
                        method=MappingMethod.STABLE_ID,
                        confidence=MappingConfidence.HIGH,
                        reason="Matched by Sleeper stable ESPN ID",
                    )
                )
                continue

            name_key = normalize_player_name(sleeper_player.full_name)
            team_key = normalize_team(sleeper_player.team)
            team_matches = (
                by_name_team.get((name_key, team_key), []) if team_key is not None else []
            )
            if len(team_matches) == 1:
                match = team_matches[0]
                mapping_results.append(
                    PlayerMapping(
                        sleeper_id=sleeper_player.sleeper_id,
                        espn_id=match.provider_id,
                        method=MappingMethod.NORMALIZED_NAME_TEAM,
                        confidence=MappingConfidence.MEDIUM,
                        reason="Matched by normalized name and team",
                    )
                )
                continue
            if len(team_matches) > 1:
                mapping_results.append(
                    self._unresolved(
                        sleeper_player,
                        "Multiple ESPN players matched normalized name and team",
                        team_matches,
                    )
                )
                continue

            name_matches = by_name.get(name_key, [])
            if len(name_matches) == 1:
                match = name_matches[0]
                mapping_results.append(
                    PlayerMapping(
                        sleeper_id=sleeper_player.sleeper_id,
                        espn_id=match.provider_id,
                        method=MappingMethod.NORMALIZED_NAME_ONLY,
                        confidence=MappingConfidence.LOW,
                        reason="Matched by unique normalized name without team confirmation",
                    )
                )
                continue
            reason = (
                "Multiple ESPN players matched normalized name"
                if len(name_matches) > 1
                else "No ESPN player matched the available identity evidence"
            )
            mapping_results.append(self._unresolved(sleeper_player, reason, name_matches))

        return MappingReport(tuple(mapping_results), tuple(warnings))

    @staticmethod
    def _unresolved(
        sleeper_player: SleeperPlayerIdentity,
        reason: str,
        candidates: Iterable[ProviderPlayer],
    ) -> PlayerMapping:
        return PlayerMapping(
            sleeper_id=sleeper_player.sleeper_id,
            espn_id=None,
            method=MappingMethod.UNRESOLVED,
            confidence=MappingConfidence.NONE,
            reason=reason,
            candidate_ids=tuple(player.provider_id for player in candidates),
        )
