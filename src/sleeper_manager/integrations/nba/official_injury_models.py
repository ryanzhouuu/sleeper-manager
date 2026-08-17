from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo

from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata

OFFICIAL_INJURY_REPORT_BASE_URL = "https://ak-static.cms.nba.com/referee/injury"
EASTERN_TIME = ZoneInfo("America/New_York")
REPORT_SCHEMA_VERSION = "2"
PARSED_REPORT_CACHE_SCHEMA_VERSION = "2"


class OfficialInjuryReportError(RuntimeError):
    pass


class ReportSubmissionStatus(StrEnum):
    SUBMITTED = "submitted"
    NOT_YET_SUBMITTED = "not_yet_submitted"


@dataclass(frozen=True, slots=True)
class OfficialInjuryReportEntry:
    game_date: date
    game_time: time
    matchup: str
    team_abbreviation: str
    team_name: str
    player_name: str
    status: AvailabilityStatus
    reason: str | None


@dataclass(frozen=True, slots=True)
class OfficialTeamReportStatus:
    game_date: date
    game_time: time
    matchup: str
    team_abbreviation: str
    team_name: str
    status: ReportSubmissionStatus


@dataclass(frozen=True, slots=True)
class OfficialInjuryReportSnapshot:
    published_at: datetime
    entries: tuple[OfficialInjuryReportEntry, ...]
    team_statuses: tuple[OfficialTeamReportStatus, ...]
    source: SourceMetadata

    @property
    def not_yet_submitted_teams(self) -> tuple[str, ...]:
        return tuple(
            status.team_abbreviation
            for status in self.team_statuses
            if status.status is ReportSubmissionStatus.NOT_YET_SUBMITTED
        )


def serialize_official_injury_report_snapshot(
    snapshot: OfficialInjuryReportSnapshot,
) -> dict[str, object]:
    return {
        "published_at": snapshot.published_at.isoformat(),
        "entries": [
            {
                "game_date": entry.game_date.isoformat(),
                "game_time": entry.game_time.isoformat(),
                "matchup": entry.matchup,
                "team_abbreviation": entry.team_abbreviation,
                "team_name": entry.team_name,
                "player_name": entry.player_name,
                "status": entry.status.value,
                "reason": entry.reason,
            }
            for entry in snapshot.entries
        ],
        "team_statuses": [
            {
                "game_date": status.game_date.isoformat(),
                "game_time": status.game_time.isoformat(),
                "matchup": status.matchup,
                "team_abbreviation": status.team_abbreviation,
                "team_name": status.team_name,
                "status": status.status.value,
            }
            for status in snapshot.team_statuses
        ],
        "source": _serialize_source_metadata(snapshot.source),
    }


def deserialize_official_injury_report_snapshot(
    payload: Mapping[str, object],
) -> OfficialInjuryReportSnapshot:
    entries_value = payload.get("entries")
    team_statuses_value = payload.get("team_statuses")
    if not isinstance(entries_value, list) or not isinstance(team_statuses_value, list):
        raise ValueError("Parsed injury report cache has invalid entry collections")
    return OfficialInjuryReportSnapshot(
        published_at=_parse_datetime(payload, "published_at"),
        entries=tuple(
            _deserialize_report_entry(_as_mapping(value, "entry")) for value in entries_value
        ),
        team_statuses=tuple(
            _deserialize_team_status(_as_mapping(value, "team status"))
            for value in team_statuses_value
        ),
        source=_deserialize_source_metadata(_as_mapping(payload.get("source"), "source")),
    )


def _serialize_source_metadata(source: SourceMetadata) -> dict[str, object]:
    return {
        "provider": source.provider,
        "provider_id": source.provider_id,
        "retrieved_at": source.retrieved_at.isoformat(),
        "source_updated_at": source.source_updated_at.isoformat()
        if source.source_updated_at is not None
        else None,
        "schema_version": source.schema_version,
        "content_hash": source.content_hash,
    }


def _deserialize_source_metadata(payload: Mapping[str, object]) -> SourceMetadata:
    source_updated_at = payload.get("source_updated_at")
    content_hash = payload.get("content_hash")
    if source_updated_at is not None and not isinstance(source_updated_at, str):
        raise ValueError("Parsed injury report cache has invalid source_updated_at")
    if content_hash is not None and not isinstance(content_hash, str):
        raise ValueError("Parsed injury report cache has invalid content_hash")
    return SourceMetadata(
        provider=_string(payload, "provider"),
        provider_id=_string(payload, "provider_id"),
        retrieved_at=datetime.fromisoformat(_string(payload, "retrieved_at")),
        source_updated_at=datetime.fromisoformat(source_updated_at)
        if source_updated_at is not None
        else None,
        schema_version=_string(payload, "schema_version"),
        content_hash=content_hash,
    )


def _deserialize_report_entry(payload: Mapping[str, object]) -> OfficialInjuryReportEntry:
    reason = payload.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("Parsed injury report cache has invalid entry reason")
    return OfficialInjuryReportEntry(
        game_date=date.fromisoformat(_string(payload, "game_date")),
        game_time=time.fromisoformat(_string(payload, "game_time")),
        matchup=_string(payload, "matchup"),
        team_abbreviation=_string(payload, "team_abbreviation"),
        team_name=_string(payload, "team_name"),
        player_name=_string(payload, "player_name"),
        status=AvailabilityStatus(_string(payload, "status")),
        reason=reason,
    )


def _deserialize_team_status(payload: Mapping[str, object]) -> OfficialTeamReportStatus:
    return OfficialTeamReportStatus(
        game_date=date.fromisoformat(_string(payload, "game_date")),
        game_time=time.fromisoformat(_string(payload, "game_time")),
        matchup=_string(payload, "matchup"),
        team_abbreviation=_string(payload, "team_abbreviation"),
        team_name=_string(payload, "team_name"),
        status=ReportSubmissionStatus(_string(payload, "status")),
    )


def _as_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Parsed injury report cache has invalid {label}")
    return value


def _string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"Parsed injury report cache has invalid {key}")
    return value


def _parse_datetime(payload: Mapping[str, object], key: str) -> datetime:
    return datetime.fromisoformat(_string(payload, key))
