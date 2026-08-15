from __future__ import annotations

import hashlib
import importlib
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
REPORT_SCHEMA_VERSION = "2"

_REPORT_HEADER = re.compile(
    r"^Injury Report:\s*(?P<date>\d{2}/\d{2}/\d{2})\s+(?P<time>\d{1,2}:\d{2}\s+[AP]M)$"
)
_GAME_HEADER = re.compile(
    r"^(?:(?P<date>\d{2}/\d{2}/\d{4})\s+)?"
    r"(?:(?P<time>\d{1,2}:\d{2})\s+\(ET\)\s+)?"
    r"(?P<away>[A-Z]{3})@(?P<home>[A-Z]{3})\s+(?P<rest>.+)$"
)
_PLAYER_ROW = re.compile(
    r"^(?P<name>.+?,\s*.+?)\s+"
    r"(?P<status>Out|Doubtful|Questionable|Probable|Available)"
    r"(?:\s+(?P<reason>.*))?$"
)
_DATE_TOKEN = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_TIME_TOKEN = re.compile(r"^\d{1,2}:\d{2}$")
_MATCHUP_TOKEN = re.compile(r"^[A-Z]{3}@[A-Z]{3}$")
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
        f"{OFFICIAL_INJURY_REPORT_BASE_URL}/Injury-Report_{local_time:%Y-%m-%d}_{filename_time}.pdf"
    )


def official_injury_report_minute_url(published_at: datetime) -> str:
    local_time = _as_eastern(published_at)
    filename_time = local_time.strftime("%I_%M%p")
    return (
        f"{OFFICIAL_INJURY_REPORT_BASE_URL}/Injury-Report_{local_time:%Y-%m-%d}_{filename_time}.pdf"
    )


def official_injury_report_urls(published_at: datetime) -> tuple[str, ...]:
    """Return compatible first-party URLs, preferring the legacy archive slot."""
    minute_url = official_injury_report_minute_url(published_at)
    if _as_eastern(published_at).minute != 30:
        return (minute_url,)
    return (official_injury_report_url(published_at), minute_url)


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


def _append_reason(entries: list[OfficialInjuryReportEntry], continuation: str) -> None:
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
    if "Injury\nReport:" in text:
        text = _normalize_tokenized_report_text(text)
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
            inherited_game_date = current_game[0] if current_game else report_date or date.min
            game_date = _parse_game_date(game_match.group("date"), inherited_game_date)
            game_time_text = game_match.group("time")
            if game_time_text is None:
                if current_game is None:
                    raise OfficialInjuryReportError(
                        f"Game line has no time and no prior game context: {line!r}"
                    )
                game_time = current_game[1]
            else:
                game_time = _parse_game_time(game_time_text)
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


def _tokenized_report_header(tokens: list[str], index: int) -> tuple[str, int] | None:
    if index + 4 >= len(tokens) or tokens[index : index + 2] != ["Injury", "Report:"]:
        return None
    if not re.fullmatch(r"\d{2}/\d{2}/\d{2}", tokens[index + 2]):
        return None
    if not _TIME_TOKEN.match(tokens[index + 3]) or tokens[index + 4] not in {"AM", "PM"}:
        return None
    return (
        f"Injury Report: {tokens[index + 2]} {tokens[index + 3]} {tokens[index + 4]}",
        index + 5,
    )


def _tokenized_game_header(
    tokens: list[str], index: int
) -> tuple[str | None, str, str, int] | None:
    start = index
    game_date: str | None = None
    if index < len(tokens) and _DATE_TOKEN.match(tokens[index]):
        game_date = tokens[index]
        index += 1
    if index + 2 >= len(tokens):
        return None
    if not _TIME_TOKEN.match(tokens[index]) or tokens[index + 1] != "(ET)":
        return None
    matchup = tokens[index + 2]
    if not _MATCHUP_TOKEN.match(matchup):
        return None
    return game_date, tokens[start + (1 if game_date else 0)], matchup, index + 3


def _tokenized_team(tokens: list[str], index: int, matchup: str) -> tuple[str, str, int] | None:
    for abbreviation in matchup.split("@"):
        team_name = NBA_TEAM_NAMES.get(abbreviation.casefold())
        if team_name is None:
            continue
        words = team_name.split()
        if tokens[index : index + len(words)] == words:
            return abbreviation.casefold(), team_name, index + len(words)
    return None


def _tokenized_page(tokens: list[str], index: int) -> int | None:
    if (
        index + 3 < len(tokens)
        and tokens[index] == "Page"
        and tokens[index + 2] == "of"
        and tokens[index + 1].isdigit()
        and tokens[index + 3].isdigit()
    ):
        return index + 4
    return None


def _tokenized_column_header(tokens: list[str], index: int) -> int | None:
    expected = [
        "Game",
        "Date",
        "Game",
        "Time",
        "Matchup",
        "Team",
        "Player",
        "Name",
        "Current",
        "Status",
        "Reason",
    ]
    if tokens[index : index + len(expected)] == expected:
        return index + len(expected)
    return None


def _tokenized_boundary(tokens: list[str], index: int, matchup: str | None) -> bool:
    if _tokenized_report_header(tokens, index) is not None:
        return True
    if _tokenized_page(tokens, index) is not None:
        return True
    if _tokenized_game_header(tokens, index) is not None:
        return True
    if (
        _MATCHUP_TOKEN.match(tokens[index])
        and _tokenized_team(tokens, index + 1, tokens[index]) is not None
    ):
        return True
    if matchup is not None and _tokenized_team(tokens, index, matchup) is not None:
        return True
    if tokens[index].endswith(","):
        return any(
            token.casefold() in _SUPPORTED_STATUSES
            for token in tokens[index + 1 : min(index + 5, len(tokens))]
        )
    return False


def _tokenized_player_line(
    tokens: list[str], index: int, matchup: str | None
) -> tuple[str, int] | None:
    status_index: int | None = None
    for candidate in range(index, len(tokens)):
        if candidate != index and (
            _tokenized_report_header(tokens, candidate) is not None
            or _tokenized_page(tokens, candidate) is not None
            or _tokenized_game_header(tokens, candidate) is not None
            or (
                _MATCHUP_TOKEN.match(tokens[candidate])
                and _tokenized_team(tokens, candidate + 1, tokens[candidate]) is not None
            )
            or (matchup is not None and _tokenized_team(tokens, candidate, matchup) is not None)
        ):
            break
        if tokens[candidate].casefold() in _SUPPORTED_STATUSES:
            status_index = candidate
            break
    if status_index is None:
        return None
    end = status_index + 1
    while end < len(tokens) and not _tokenized_boundary(tokens, end, matchup):
        end += 1
    name = " ".join(tokens[index:status_index])
    status = tokens[status_index]
    reason = " ".join(tokens[status_index + 1 : end])
    line = f"{name} {status}" + (f" {reason}" if reason else "")
    return line, end


def _normalize_tokenized_report_text(text: str) -> str:
    """Restore logical rows from pypdf's one-word-per-line extraction mode."""
    tokens = [token.strip() for token in text.splitlines() if token.strip()]
    lines: list[str] = []
    index = 0
    current_matchup: str | None = None
    current_game_time: str | None = None
    while index < len(tokens):
        report_header = _tokenized_report_header(tokens, index)
        if report_header is not None:
            lines.append(report_header[0])
            index = report_header[1]
            continue
        page_end = _tokenized_page(tokens, index)
        if page_end is not None:
            lines.append(" ".join(tokens[index:page_end]))
            index = page_end
            continue
        column_end = _tokenized_column_header(tokens, index)
        if column_end is not None:
            lines.append(" ".join(tokens[index:column_end]))
            index = column_end
            continue
        game_header = _tokenized_game_header(tokens, index)
        if game_header is not None:
            game_date, game_time, current_matchup, next_index = game_header
            team = _tokenized_team(tokens, next_index, current_matchup)
            if team is None:
                raise OfficialInjuryReportError(
                    f"Unknown team prefix in tokenized game line near {tokens[index : index + 8]!r}"
                )
            prefix = f"{game_date + ' ' if game_date else ''}{game_time} (ET) "
            game_line = f"{prefix}{current_matchup} {team[1]}"
            if tokens[team[2] : team[2] + 3] == ["NOT", "YET", "SUBMITTED"]:
                game_line += " NOT YET SUBMITTED"
                index = team[2] + 3
            else:
                index = team[2]
            lines.append(game_line)
            current_game_time = game_time
            continue
        if current_game_time is not None and _MATCHUP_TOKEN.match(tokens[index]):
            matchup = tokens[index]
            team = _tokenized_team(tokens, index + 1, matchup)
            if team is not None:
                current_matchup = matchup
                matchup_line = f"{current_game_time} (ET) {matchup} {team[1]}"
                if tokens[team[2] : team[2] + 3] == ["NOT", "YET", "SUBMITTED"]:
                    matchup_line += " NOT YET SUBMITTED"
                    index = team[2] + 3
                else:
                    index = team[2]
                lines.append(matchup_line)
                continue
        if current_matchup is not None:
            team = _tokenized_team(tokens, index, current_matchup)
            if team is not None:
                team_end = team[2]
                if tokens[team_end : team_end + 3] == ["NOT", "YET", "SUBMITTED"]:
                    lines.append(f"{team[1]} NOT YET SUBMITTED")
                    index = team_end + 3
                    continue
                player_line = _tokenized_player_line(tokens, team_end, current_matchup)
                if player_line is not None:
                    lines.append(f"{team[1]} {player_line[0]}")
                    index = player_line[1]
                    continue
        player_line = _tokenized_player_line(tokens, index, current_matchup)
        if player_line is not None:
            lines.append(player_line[0])
            index = player_line[1]
            continue
        index += 1
    return "\n".join(lines)


def extract_official_injury_report_text(pdf_bytes: bytes) -> str:
    try:
        pypdf = importlib.import_module("pypdf")
    except ImportError as error:
        raise OfficialInjuryReportError(
            "Official injury-report ingestion requires the pypdf dependency"
        ) from error
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    text = "\n".join(str(page.extract_text() or "") for page in reader.pages)
    if "Injury\nReport:" in text:
        return _normalize_tokenized_report_text(text)
    return text


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
        response: httpx.Response | None = None
        url: str | None = None
        for candidate_url in official_injury_report_urls(published_at):
            candidate_response = await self._client.get(candidate_url)
            if candidate_response.status_code in {403, 404}:
                response = candidate_response
                url = candidate_url
                continue
            if candidate_response.status_code != 200:
                raise OfficialInjuryReportError(
                    "Official injury report request failed with "
                    f"HTTP {candidate_response.status_code}: {candidate_url}"
                )
            response = candidate_response
            url = candidate_url
            break
        if response is None or response.status_code != 200 or url is None:
            status = response.status_code if response is not None else "unknown"
            raise OfficialInjuryReportError(
                f"Official injury report request failed with HTTP {status}: {url}"
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
