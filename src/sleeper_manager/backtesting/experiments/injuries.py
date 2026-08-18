from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

import httpx

from sleeper_manager.domain.nba import ProviderPlayer, ScheduledGame, SourceMetadata
from sleeper_manager.integrations.nba.official_injury_mapping import (
    HistoricalPlayerAvailability,
    InjuryMappingCategory,
    InjuryMappingDiagnostic,
    map_official_injury_report,
)
from sleeper_manager.integrations.nba.official_injury_models import (
    EASTERN_TIME,
    PARSED_REPORT_CACHE_SCHEMA_VERSION,
    REPORT_SCHEMA_VERSION,
    OfficialInjuryReportSnapshot,
    deserialize_official_injury_report_snapshot,
    serialize_official_injury_report_snapshot,
)
from sleeper_manager.integrations.nba.official_injury_parser import (
    extract_official_injury_report_text,
    official_injury_report_urls,
    parse_official_injury_report_text,
)


class InjuryArchiveError(RuntimeError):
    """Raised when historical injury evidence cannot be acquired."""

    pass


class HTTPResponse(Protocol):
    status_code: int
    content: bytes


class HTTPClient(Protocol):
    def get(self, url: str) -> HTTPResponse: ...


SnapshotParser = Callable[[bytes, SourceMetadata], OfficialInjuryReportSnapshot]


@dataclass(frozen=True, slots=True)
class InjuryReportSelection:
    requested_at: datetime
    selected_at: datetime | None
    url: str | None
    sha256: str | None
    cache_path: str | None
    attempts: int
    unavailable_candidates: tuple[tuple[datetime, str], ...]


@dataclass(frozen=True, slots=True)
class InjuryArchiveResult:
    snapshots: tuple[OfficialInjuryReportSnapshot, ...]
    availability: tuple[HistoricalPlayerAvailability, ...]
    selections: tuple[InjuryReportSelection, ...]
    unresolved_identity_count: int
    mapping_warning_count: int
    mapping_diagnostics: tuple[InjuryMappingDiagnostic, ...] = ()


def latest_report_timestamp(cutoff: datetime) -> datetime:
    if cutoff.tzinfo is None:
        raise InjuryArchiveError("Decision cutoff must be timezone-aware")
    eastern = cutoff.astimezone(EASTERN_TIME)
    if eastern.minute >= 30:
        selected = eastern.replace(minute=30, second=0, microsecond=0)
    else:
        selected = (eastern - timedelta(hours=1)).replace(
            minute=30,
            second=0,
            microsecond=0,
        )
    return selected.astimezone(UTC)


def requested_report_timestamps(
    games: Iterable[ScheduledGame],
    *,
    cutoff_minutes: int = 30,
) -> tuple[datetime, ...]:
    if cutoff_minutes < 0:
        raise InjuryArchiveError("Decision cutoff minutes must be non-negative")
    return tuple(sorted({game.start_time - timedelta(minutes=cutoff_minutes) for game in games}))


def acquire_injury_archive(
    games: Iterable[ScheduledGame],
    provider_players: Iterable[ProviderPlayer],
    cache_dir: Path,
    *,
    retrieved_at: datetime,
    historical_player_ids_by_date_team: Mapping[tuple[date, str], frozenset[str]] | None = None,
    client: HTTPClient | None = None,
    parser: SnapshotParser | None = None,
    max_lookback_hours: int = 24,
    retry_attempts: int = 4,
    request_interval_seconds: float = 0.1,
    sleeper: Callable[[float], None] = time.sleep,
) -> InjuryArchiveResult:
    if retrieved_at.tzinfo is None:
        raise InjuryArchiveError("Injury archive retrieval timestamp must be timezone-aware")
    if max_lookback_hours < 0:
        raise InjuryArchiveError("Injury archive lookback must be non-negative")
    if retry_attempts <= 0:
        raise InjuryArchiveError("Injury archive retry attempts must be positive")
    if request_interval_seconds < 0:
        raise InjuryArchiveError("Injury archive request interval must be non-negative")
    cache_dir.mkdir(parents=True, exist_ok=True)
    owned_client = client is None
    http_client = client or cast(HTTPClient, httpx.Client(timeout=60, follow_redirects=True))
    snapshot_parser = parser or _parse_snapshot
    use_parsed_cache = parser is None
    snapshots_by_nominal_time: dict[datetime, OfficialInjuryReportSnapshot] = {}
    content_by_nominal_time: dict[datetime, tuple[str, str, str]] = {}
    unavailable: dict[tuple[datetime, str], str] = {}
    selections: list[InjuryReportSelection] = []
    try:
        for requested_at in requested_report_timestamps(games):
            selected: OfficialInjuryReportSnapshot | None = None
            selected_metadata: tuple[str, str, str] | None = None
            attempts = 0
            unavailable_candidates: list[tuple[datetime, str]] = []
            first_candidate_at = latest_report_timestamp(requested_at)
            for hours in range(max_lookback_hours + 1):
                attempts += 1
                candidate_at = first_candidate_at - timedelta(hours=hours)
                cached = snapshots_by_nominal_time.get(candidate_at)
                if cached is not None:
                    if cached.published_at <= requested_at:
                        selected = cached
                        selected_metadata = content_by_nominal_time[candidate_at]
                        break
                    unavailable_candidates.append((candidate_at, "post_cutoff"))
                    continue
                for source_index, url in enumerate(official_injury_report_urls(candidate_at)):
                    unavailable_key = candidate_at, url
                    if unavailable_key in unavailable:
                        unavailable_candidates.append((candidate_at, unavailable[unavailable_key]))
                        continue
                    cache_path = cache_dir / _cache_filename(candidate_at, source_index)
                    if cache_path.is_file():
                        content = cache_path.read_bytes()
                    else:
                        response = _request_with_retry(
                            http_client,
                            url,
                            attempts=retry_attempts,
                            sleeper=sleeper,
                        )
                        sleeper(request_interval_seconds)
                        if response.status_code in {403, 404}:
                            status = f"http_{response.status_code}"
                            unavailable[unavailable_key] = status
                            unavailable_candidates.append((candidate_at, status))
                            continue
                        if response.status_code != 200:
                            raise InjuryArchiveError(
                                "Official injury archive returned "
                                f"HTTP {response.status_code}: {url}"
                            )
                        content = response.content
                        cache_path.write_bytes(content)
                    content_hash = hashlib.sha256(content).hexdigest()
                    source = SourceMetadata(
                        provider="nba_official_injury_report",
                        provider_id=url,
                        retrieved_at=retrieved_at.astimezone(UTC),
                        source_updated_at=candidate_at,
                        schema_version=REPORT_SCHEMA_VERSION,
                        content_hash=content_hash,
                    )
                    try:
                        snapshot, parsed_cache_hit = _load_snapshot(
                            cache_path,
                            content,
                            source,
                            snapshot_parser,
                            use_parsed_cache=use_parsed_cache,
                        )
                    except Exception as error:
                        raise InjuryArchiveError(
                            f"Could not parse cached official injury report {cache_path}"
                        ) from error
                    if not _same_archive_hour(snapshot.published_at, candidate_at):
                        raise InjuryArchiveError(
                            f"Official injury report timestamp mismatch for {cache_path}"
                        )
                    actual_source = replace(source, source_updated_at=snapshot.published_at)
                    snapshot = replace(snapshot, source=actual_source)
                    if use_parsed_cache and not parsed_cache_hit:
                        _write_parsed_snapshot(cache_path, content_hash, snapshot)
                    snapshots_by_nominal_time[candidate_at] = snapshot
                    content_by_nominal_time[candidate_at] = url, content_hash, str(cache_path)
                    if snapshot.published_at > requested_at:
                        unavailable_candidates.append((candidate_at, "post_cutoff"))
                        break
                    selected = snapshot
                    selected_metadata = url, content_hash, str(cache_path)
                    break
                if selected is not None or candidate_at in snapshots_by_nominal_time:
                    cached_snapshot = snapshots_by_nominal_time[candidate_at]
                    if cached_snapshot.published_at <= requested_at:
                        break
            selections.append(
                InjuryReportSelection(
                    requested_at=requested_at,
                    selected_at=selected.published_at if selected else None,
                    url=selected_metadata[0] if selected_metadata else None,
                    sha256=selected_metadata[1] if selected_metadata else None,
                    cache_path=selected_metadata[2] if selected_metadata else None,
                    attempts=attempts,
                    unavailable_candidates=tuple(unavailable_candidates),
                )
            )
    finally:
        if owned_client and isinstance(http_client, httpx.Client):
            http_client.close()

    players = tuple(provider_players)
    availability: list[HistoricalPlayerAvailability] = []
    unresolved = 0
    warnings = 0
    diagnostic_counts: Counter[tuple[InjuryMappingCategory, int, str, str]] = Counter()
    snapshots_by_timestamp = {
        snapshot.published_at: snapshot for snapshot in snapshots_by_nominal_time.values()
    }
    for snapshot in snapshots_by_timestamp.values():
        mapping = map_official_injury_report(
            snapshot,
            players,
            historical_player_ids_by_date_team=historical_player_ids_by_date_team,
        )
        availability.extend(mapping.availability)
        unresolved += len(mapping.unresolved)
        warnings += len(mapping.warnings)
        for diagnostic in mapping.diagnostics:
            diagnostic_key = (
                diagnostic.category,
                diagnostic.season,
                diagnostic.team_abbreviation,
                diagnostic.normalized_name,
            )
            diagnostic_counts[diagnostic_key] += diagnostic.count
    return InjuryArchiveResult(
        snapshots=tuple(
            snapshots_by_timestamp[timestamp] for timestamp in sorted(snapshots_by_timestamp)
        ),
        availability=tuple(
            sorted(
                availability,
                key=lambda record: (
                    record.available_as_of,
                    record.game_date,
                    record.player_id,
                ),
            )
        ),
        selections=tuple(selections),
        unresolved_identity_count=unresolved,
        mapping_warning_count=warnings,
        mapping_diagnostics=tuple(
            InjuryMappingDiagnostic(
                category=category,
                season=season,
                team_abbreviation=team_abbreviation,
                normalized_name=normalized_name,
                count=count,
            )
            for (category, season, team_abbreviation, normalized_name), count in sorted(
                diagnostic_counts.items(),
                key=lambda item: item[0],
            )
        ),
    )


def _cache_filename(published_at: datetime, source_index: int = 0) -> str:
    suffix = "" if source_index == 0 else "-minute"
    return f"injury-report-{published_at.astimezone(UTC):%Y%m%dT%H%MZ}{suffix}.pdf"


def _load_snapshot(
    cache_path: Path,
    content: bytes,
    source: SourceMetadata,
    parser: SnapshotParser,
    *,
    use_parsed_cache: bool,
) -> tuple[OfficialInjuryReportSnapshot, bool]:
    content_hash = source.content_hash
    if use_parsed_cache and content_hash is not None:
        cached = _read_parsed_snapshot(cache_path, content_hash)
        if cached is not None:
            return cached, True
    return parser(content, source), False


def _parsed_cache_path(pdf_path: Path) -> Path:
    return pdf_path.with_suffix(".json")


def _read_parsed_snapshot(
    pdf_path: Path,
    content_hash: str,
) -> OfficialInjuryReportSnapshot | None:
    path = _parsed_cache_path(pdf_path)
    try:
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict):
            return None
        if payload.get("cache_schema_version") != PARSED_REPORT_CACHE_SCHEMA_VERSION:
            return None
        if payload.get("pdf_sha256") != content_hash:
            return None
        snapshot_payload = payload.get("snapshot")
        if not isinstance(snapshot_payload, dict):
            return None
        return deserialize_official_injury_report_snapshot(snapshot_payload)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _write_parsed_snapshot(
    pdf_path: Path,
    content_hash: str,
    snapshot: OfficialInjuryReportSnapshot,
) -> None:
    path = _parsed_cache_path(pdf_path)
    temporary_path = path.with_name(f"{path.name}.tmp")
    payload = {
        "cache_schema_version": PARSED_REPORT_CACHE_SCHEMA_VERSION,
        "pdf_sha256": content_hash,
        "snapshot": serialize_official_injury_report_snapshot(snapshot),
    }
    try:
        temporary_path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _same_archive_hour(published_at: datetime, nominal_at: datetime) -> bool:
    published = published_at.astimezone(EASTERN_TIME)
    nominal = nominal_at.astimezone(EASTERN_TIME)
    return published.date() == nominal.date() and published.hour == nominal.hour


def _request_with_retry(
    client: HTTPClient,
    url: str,
    *,
    attempts: int,
    sleeper: Callable[[float], None],
) -> HTTPResponse:
    response: HTTPResponse | None = None
    for attempt in range(attempts):
        response = client.get(url)
        retryable = response.status_code == 429 or response.status_code >= 500
        if not retryable or attempt == attempts - 1:
            return response
        sleeper(float(2**attempt))
    if response is None:
        raise InjuryArchiveError("Injury archive request did not execute")
    return response


def _parse_snapshot(content: bytes, source: SourceMetadata) -> OfficialInjuryReportSnapshot:
    return parse_official_injury_report_text(
        extract_official_injury_report_text(content),
        source=replace(source, source_updated_at=None),
    )
