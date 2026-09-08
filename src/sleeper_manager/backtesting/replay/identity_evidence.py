"""Recover absent provider IDs from attributed rosters without importing current teams."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, AwareDatetime, BaseModel, ConfigDict, Field

from sleeper_manager.backtesting.replay.inputs import SourceFingerprint, source_fingerprint
from sleeper_manager.backtesting.replay.team_week_sources import HistoricalTeamWeekBundleError
from sleeper_manager.domain.nba import ProviderPlayer, SourceMetadata
from sleeper_manager.integrations.nba.mapping import normalize_player_name


class _RosterSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    url: str = Field(
        pattern=r"^https://site\.api\.espn\.com/apis/site/v2/sports/basketball/nba/teams/\d+/roster$"
    )
    retrieved_at: AwareDatetime
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _IdentityLedger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["espn-roster-identities-v1"]
    sources: tuple[_RosterSource, ...]


class _Athlete(BaseModel):
    id: str = Field(min_length=1)
    full_name: str = Field(validation_alias=AliasChoices("fullName", "displayName"), min_length=1)
    date_of_birth: AwareDatetime | None = Field(alias="dateOfBirth", default=None)


class _Roster(BaseModel):
    athletes: tuple[_Athlete, ...]


@dataclass(frozen=True, slots=True)
class IdentityEvidence:
    """Stable identity candidates and provenance; no historical affiliation claims."""

    players: tuple[ProviderPlayer, ...] = ()
    source_fingerprints: tuple[SourceFingerprint, ...] = ()


def load_identity_evidence(
    path: Path,
    catalog: Mapping[str, Mapping[str, object]],
    unresolved_player_ids: Sequence[str],
    existing_players: Sequence[ProviderPlayer] = (),
) -> IdentityEvidence:
    """Require unique ID matches by full name and birth date across all supplied sources.

    Missing or conflicting matches stay unresolved. Malformed sources and changed
    hashes raise. Current teams and active status never enter recovered identities.
    """
    payload = path.read_bytes()
    ledger = _IdentityLedger.model_validate_json(payload)
    records: list[tuple[_Athlete, SourceMetadata]] = []
    fingerprints = [
        source_fingerprint("provider-identity-ledger", payload, version=ledger.schema_version)
    ]
    seen_sources: set[str] = set()
    for item in ledger.sources:
        raw = (path.parent / item.path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != item.sha256:
            raise HistoricalTeamWeekBundleError("Provider identity source hash mismatch")
        if item.sha256 in seen_sources:
            continue
        seen_sources.add(item.sha256)
        roster = _Roster.model_validate_json(raw)
        source = SourceMetadata(
            "espn-identity-only",
            item.url,
            item.retrieved_at,
            schema_version=ledger.schema_version,
            content_hash=item.sha256,
        )
        records.extend((athlete, source) for athlete in roster.athletes)
        fingerprints.append(
            SourceFingerprint(f"provider-identity-roster:{item.sha256}", item.sha256, "json-v1")
        )
    players: dict[str, ProviderPlayer] = {}
    for sleeper_id in unresolved_player_ids:
        player = catalog.get(sleeper_id, {})
        full_name, birth_date = player.get("full_name"), player.get("birth_date")
        if not isinstance(full_name, str) or not isinstance(birth_date, str):
            continue
        try:
            born = date.fromisoformat(birth_date)
        except ValueError:
            continue
        matches = [
            (athlete, source)
            for athlete, source in records
            if normalize_player_name(athlete.full_name) == normalize_player_name(full_name)
            and athlete.date_of_birth is not None
            and athlete.date_of_birth.date() == born
        ]
        if len({athlete.id for athlete, _ in matches}) != 1:
            continue
        athlete, source = matches[0]
        conflicting = any(
            other.id == athlete.id
            and (
                normalize_player_name(other.full_name) != normalize_player_name(full_name)
                or other.date_of_birth is None
                or other.date_of_birth.date() != born
            )
            for other, _ in records
        )
        conflicting |= any(
            other.provider_id == athlete.id
            and normalize_player_name(other.full_name) != normalize_player_name(full_name)
            for other in existing_players
        )
        if not conflicting:
            players[athlete.id] = ProviderPlayer(athlete.id, full_name, None, None, None, source)
    return IdentityEvidence(tuple(players[key] for key in sorted(players)), tuple(fingerprints))
