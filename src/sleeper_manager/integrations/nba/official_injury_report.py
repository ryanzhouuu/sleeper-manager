from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo

import httpx

from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.integrations.nba.mapping import NBA_TEAM_NAMES

OFFICIAL_INJURY_REPORT_BASE_URL = "https://ak-static.cms.nba.com/referee/injury"
EASTERN_TIME = ZoneInfo("America/New_York")
REPORT_SCHEMA_VERSION = "1"

_REPORT_HEADER = re.compile(
    r"^Injury Report:\s*(?P<date>\d{2}/\d{2}/\d{2})\s+(?P<time>\d{1,2}:\d{2}\s+[AP]M)$"
)
_GAME_HEADER = re.compile(
    r"^(?:(?P<date>\d{2}/\d{2}/\d{4})\s+)?"
    r"(?P<time>\d{1,2}:\d{2})\s+\(ET\)\s+"
    r"(?P<away>[A-Z]{3})@(?P<home>[A-Z]{3})\s+(?P<rest>.+)$"
)
_PLAYER_ROW = re.compile(
    r"^(?P<name>.+?,\s*.+?)\s+"
    r"(?P<status>Out|Doubtful|Questionable|Probable|Available)"
    r"(?:\s+(?P<reason>.*))?$"
)
_SUPPORTED_STATUSES = {
    "available": AvailabilityStatus.AVAILABLE,
    "probable": AvailabilityStatus.PROBABLE,
    "questionable": AvailabilityStatus.QUESTIONABLE,
    "doubtful": AvailabilityStatus.DOUBTFUL,
    "out": AvailabilityStatus.OUT,
}


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


@dataclass(frozen=True, slots=True)
class OfficialReportCoverage:
    requested_published_at: tuple[datetime, ...]
    received_published_at: tuple[datetime, ...]
    missing_published_at: tuple[datetime, ...]
    parse_errors: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing_published_at and not self.parse_errors


def official_injury_report_url(published_at: datetime) -> str:
    local_time = _as_eastern(published_at)
    if local_time.minute != 30:
        raise ValueError("Official NBA injury reports use half-hour publication timestamps")
    filename_time = local_time.strftime("%I%p")
    return (
        f"{OFFICIAL_INJURY_REPORT_BASE_URL}/Injury-Report_"
        f"{local_time:%Y-%m-%d}_{filename_time}.pdf"
    )


def _as_eastern(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Injury report timestamps must include a timezone")
    return value.astimezone(EASTERN_TIME)


def _parse_report_timestamp(value: str) -> datetime:
    parsed = datetime.strptime(value, "%m/%d/%y %I:%M %p")
    return parsed.replace(tzinfo=EASTERN_TIME).astimezone(UTC)


def _parse_game_date(value: str | None, default: date) -> date:
    if value is None:
        return default
    return datetime.strptime(value, "%m/%d/%Y").date()


def _parse_game_time(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def _team_prefix(line: str, matchup: str) -> tuple[str, str, str] | None:
    for abbreviation in matchup.split("@"):
        team_name = NBA_TEAM_NAMES.get(abbreviation.casefold())
        if team_name is None:
            continue
        if line == team_name:
            return abbreviation.casefold(), team_name, ""
        prefix = f"{team_name} "
        if line.startswith(prefix):
            return abbreviation.casefold(), team_name, line[len(prefix) :].strip()
    return None


def _append_reason(
    entries: list[OfficialInjuryReportEntry], continuation: str
) -> None:
    if not entries:
        return
    previous = entries[-1]
    reason = " ".join(part for part in (previous.reason, continuation.strip()) if part)
    entries[-1] = OfficialInjuryReportEntry(
        game_date=previous.game_date,
        game_time=previous.game_time,
        matchup=previous.matchup,
        team_abbreviation=previous.team_abbreviation,
        team_name=previous.team_name,
        player_name=previous.player_name,
        status=previous.status,
        reason=reason or None,
    )


def parse_official_injury_report_text(
    text: str, *, source: SourceMetadata
) -> OfficialInjuryReportSnapshot:
    """Parse text extracted from one official NBA injury-report PDF."""
    normalized_text = text.replace("Injury Report:", "\nInjury Report:")
    lines = [line.strip() for line in normalized_text.splitlines() if line.strip()]
    published_at: datetime | None = None
    report_date: date | None = None
    current_game: tuple[date, time, str] | None = None
    current_team: tuple[str, str] | None = None
    entries: list[OfficialInjuryReportEntry] = []
    team_statuses: list[OfficialTeamReportStatus] = []

    for line in lines:
        if line.startswith("Page ") or line == (
            "Game Date Game Time Matchup Team Player Name Current Status Reason"
        ):
            continue
        report_match = _REPORT_HEADER.match(line)
        if report_match:
            published_at = _parse_report_timestamp(
                f"{report_match.group('date')} {report_match.group('time')}"
            )
            report_date = published_at.astimezone(EASTERN_TIME).date()
            continue

        game_match = _GAME_HEADER.match(line)
        if game_match:
            game_date = _parse_game_date(game_match.group("date"), report_date or date.min)
            game_time = _parse_game_time(game_match.group("time"))
            matchup = f"{game_match.group('away')}@{game_match.group('home')}"
            team_data = _team_prefix(game_match.group("rest"), matchup)
            if team_data is None:
                raise OfficialInjuryReportError(f"Unknown team prefix in game line: {line!r}")
            current_game = (game_date, game_time, matchup)
            current_team = (team_data[0], team_data[1])
            team_statuses.append(
                OfficialTeamReportStatus(
                    game_date=game_date,
                    game_time=game_time,
                    matchup=matchup,
                    team_abbreviation=team_data[0],
                    team_name=team_data[1],
                    status=ReportSubmissionStatus.SUBMITTED,
                )
            )
            remainder = team_data[2]
            if remainder:
                _parse_team_remainder(
                    remainder,
                    current_game=current_game,
                    current_team=current_team,
                    entries=entries,
                    team_statuses=team_statuses,
                )
            continue

        if current_game is None:
            continue
        team_data = _team_prefix(line, current_game[2])
        if team_data is not None:
            current_team = (team_data[0], team_data[1])
            team_statuses.append(
                OfficialTeamReportStatus(
                    game_date=current_game[0],
                    game_time=current_game[1],
                    matchup=current_game[2],
                    team_abbreviation=team_data[0],
                    team_name=team_data[1],
                    status=ReportSubmissionStatus.SUBMITTED,
                )
            )
            if team_data[2]:
                _parse_team_remainder(
                    team_data[2],
                    current_game=current_game,
                    current_team=current_team,
                    entries=entries,
                    team_statuses=team_statuses,
                )
            continue

        if current_team is None:
            continue
        row_match = _PLAYER_ROW.match(line)
        if row_match:
            status = _SUPPORTED_STATUSES[row_match.group("status").casefold()]
            entries.append(
                OfficialInjuryReportEntry(
                    game_date=current_game[0],
                    game_time=current_game[1],
                    matchup=current_game[2],
                    team_abbreviation=current_team[0],
                    team_name=current_team[1],
                    player_name=row_match.group("name").strip(),
                    status=status,
                    reason=(row_match.group("reason") or "").strip() or None,
                )
            )
            continue

        if not line.startswith("Injury Report:"):
            _append_reason(entries, line)

    if published_at is None or report_date is None:
        raise OfficialInjuryReportError("Official injury report text has no report timestamp")
    if source.source_updated_at is not None and source.source_updated_at != published_at:
        raise OfficialInjuryReportError(
            "Source timestamp does not match the official injury report header"
        )
    return OfficialInjuryReportSnapshot(
        published_at=published_at,
        entries=tuple(entries),
        team_statuses=_deduplicate_team_statuses(team_statuses),
        source=source,
    )


def _parse_team_remainder(
    remainder: str,
    *,
    current_game: tuple[date, time, str],
    current_team: tuple[str, str],
    entries: list[OfficialInjuryReportEntry],
    team_statuses: list[OfficialTeamReportStatus],
) -> None:
    if remainder == "NOT YET SUBMITTED":
        team_statuses[-1] = OfficialTeamReportStatus(
            game_date=current_game[0],
            game_time=current_game[1],
            matchup=current_game[2],
            team_abbreviation=current_team[0],
            team_name=current_team[1],
            status=ReportSubmissionStatus.NOT_YET_SUBMITTED,
        )
        return
    row_match = _PLAYER_ROW.match(remainder)
    if row_match is None:
        raise OfficialInjuryReportError(f"Unsupported injury report row: {remainder!r}")
    entries.append(
        OfficialInjuryReportEntry(
            game_date=current_game[0],
            game_time=current_game[1],
            matchup=current_game[2],
            team_abbreviation=current_team[0],
            team_name=current_team[1],
            player_name=row_match.group("name").strip(),
            status=_SUPPORTED_STATUSES[row_match.group("status").casefold()],
            reason=(row_match.group("reason") or "").strip() or None,
        )
    )


def _deduplicate_team_statuses(
    statuses: Iterable[OfficialTeamReportStatus],
) -> tuple[OfficialTeamReportStatus, ...]:
    result: list[OfficialTeamReportStatus] = []
    seen: set[tuple[date, time, str, str]] = set()
    for status in statuses:
        key = (status.game_date, status.game_time, status.matchup, status.team_abbreviation)
        if key not in seen:
            seen.add(key)
            result.append(status)
    return tuple(result)


def extract_official_injury_report_text(pdf_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError as error:
        raise OfficialInjuryReportError(
            "Official injury-report ingestion requires the pypdf dependency"
        ) from error
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def assess_official_report_coverage(
    requested_published_at: Iterable[datetime],
    snapshots: Iterable[OfficialInjuryReportSnapshot],
    *,
    parse_errors: Iterable[str] = (),
) -> OfficialReportCoverage:
    requested = tuple(sorted(set(requested_published_at)))
    received = tuple(sorted({snapshot.published_at for snapshot in snapshots}))
    received_set = set(received)
    return OfficialReportCoverage(
        requested_published_at=requested,
        received_published_at=received,
        missing_published_at=tuple(
            timestamp for timestamp in requested if timestamp not in received_set
        ),
        parse_errors=tuple(parse_errors),
    )


class OfficialInjuryReportClient:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] | None = None,
        pdf_text_extractor: Callable[[bytes], str] | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._clock = clock or (lambda: datetime.now(UTC))
        self._pdf_text_extractor = pdf_text_extractor or extract_official_injury_report_text

    async def __aenter__(self) -> OfficialInjuryReportClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def report(self, published_at: datetime) -> OfficialInjuryReportSnapshot:
        url = official_injury_report_url(published_at)
        response = await self._client.get(url)
        if response.status_code != 200:
            raise OfficialInjuryReportError(
                f"Official injury report request failed with HTTP {response.status_code}: {url}"
            )
        retrieved_at = self._clock()
        if retrieved_at.tzinfo is None:
            raise OfficialInjuryReportError(
                "Official injury report clock must return a timezone-aware timestamp"
            )
        published_timestamp = _as_eastern(published_at).astimezone(UTC)
        source = SourceMetadata(
            provider="nba_official_injury_report",
            provider_id=url,
            retrieved_at=retrieved_at.astimezone(UTC),
            source_updated_at=published_timestamp,
            schema_version=REPORT_SCHEMA_VERSION,
            content_hash=hashlib.sha256(response.content).hexdigest(),
        )
        return parse_official_injury_report_text(
            self._pdf_text_extractor(response.content),
            source=source,
        )
