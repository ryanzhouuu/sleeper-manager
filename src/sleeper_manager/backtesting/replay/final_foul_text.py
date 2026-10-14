"""Read explicit foul events from complete official final-box layout text.

This parser does not resolve surnames or certify omitted summaries as zero.
History consumers must separately reconcile identities and attest to coverage.
"""

import re
from dataclasses import dataclass
from typing import Literal

from sleeper_manager.backtesting.replay.final_roster_text import (
    FinalRosterText,
    parse_final_roster_text,
)

FoulKind = Literal["individual", "non_unsportsmanlike", "flagrant"]
_HEADINGS: dict[str, FoulKind] = {
    "Technical fouls - Individual": "individual",
    "Technical fouls - Non-Unsportsmanlike": "non_unsportsmanlike",
    "Technical fouls - Defensive Three Seconds": "non_unsportsmanlike",
    "Technical fouls - Delay": "non_unsportsmanlike",
    "Technical fouls - Excess Timeout": "non_unsportsmanlike",
    "Technical fouls - Bench": "individual",
    "Flagrant Fouls": "flagrant",
}
_SECTION = re.compile(
    r"^\s*(Technical [Ff]ouls[^\n]*|Flagrant Fouls[^\n]*|Ejections|MEMO:[^\n]*|"
    r"Copyright[^\n]*)\s*$",
    re.MULTILINE,
)
_TEAM = re.compile(r"^\s*([^\n()]+?)\s*\((\d+)\)\s*:\s*", re.MULTILINE)
_CLOCK = r"(\d{1,2}:[0-5]\d)\s+((?:1st|2nd|3rd|4th)(?:\s+OT)?)"


@dataclass(frozen=True, slots=True)
class FinalFoulEvent:
    """Preserve each charged actor, including team and unresolved bench actors."""

    side: int
    name: str
    kind: FoulKind
    clock: str
    period: int
    detail: str

    @property
    def field(self) -> str:
        return "flagrant_fouls" if self.kind == "flagrant" else "technical_fouls"


@dataclass(frozen=True, slots=True)
class FinalFoulText:
    """Keep positive events and section presence separate from coverage attestation."""

    teams: tuple[str, str]
    roster: FinalRosterText
    events: tuple[FinalFoulEvent, ...]
    sections: tuple[FoulKind, ...]


def parse_final_foul_text(
    text: str, *, expected_teams: tuple[str, str] | None = None
) -> FinalFoulText:
    """Reject truncated, unsupported, or count-inconsistent final reports."""
    text = _bind_neutral_teams(text, expected_teams)
    if (
        re.search(
            r"Copyright \(c\) \d{4}(?:-\d{4})? NBA Properties, INC\. All Rights Reserved", text
        )
        is None
    ):
        raise ValueError("Incomplete final foul report")
    roster = parse_final_roster_text(text)
    teams = tuple(
        match.group(1).strip()
        for side in ("VISITOR", "HOME")
        if (match := re.search(rf"{side}:\s*(.+?)\s*\(\d+-\d+\)", text))
    )
    if len(teams) != 2 or {row.side for row in roster.rows} != {0, 1}:
        raise ValueError("Missing final foul report teams or roster")
    if expected_teams is not None and tuple(map(_team_name, teams)) != tuple(
        map(_team_name, expected_teams)
    ):
        raise ValueError("Final foul report team mismatch")
    summary = text.split("SCORE BY", 1)[1]
    headings = tuple(_SECTION.finditer(summary))
    events: list[FinalFoulEvent] = []
    sections: list[FoulKind] = []
    seen_titles: set[str] = set()
    for index, heading in enumerate(headings):
        title = heading.group(1).strip()
        if title.startswith(("Ejections", "MEMO:", "Copyright")):
            continue
        kind = _HEADINGS.get(title)
        if kind is None or title in seen_titles:
            raise ValueError("Unknown or duplicate final foul section")
        seen_titles.add(title)
        if kind not in sections:
            sections.append(kind)
        end = headings[index + 1].start() if index + 1 < len(headings) else len(summary)
        body = summary[heading.end() : end]
        sides = tuple(_TEAM.finditer(body))
        if len(sides) != 2:
            raise ValueError("Incomplete final foul section")
        for side, match in enumerate(sides):
            if not _normalized(teams[side]).endswith(_normalized(match.group(1))):
                raise ValueError("Final foul summary team mismatch")
            stop = sides[side + 1].start() if side == 0 else len(body)
            entries = " ".join(body[match.end() : stop].split())
            count = int(match.group(2))
            if count == 0:
                if entries != "NONE":
                    raise ValueError("Invalid zero final foul summary")
                continue
            parts = entries.split(" , ")
            if len(parts) != count:
                raise ValueError("Final foul summary count mismatch")
            team_only = title in ("Technical fouls - Delay", "Technical fouls - Excess Timeout")
            events.extend(_event(part, side, kind, team_only=team_only) for part in parts)
    return FinalFoulText((teams[0], teams[1]), roster, tuple(events), tuple(sections))


def _team_name(value: str) -> str:
    return " ".join(value.casefold().split())


def _bind_neutral_teams(text: str, expected: tuple[str, str] | None) -> str:
    """Bind unlabelled cup headers only to independently supplied away/home identities."""
    if expected is None or "VISITOR:" in text or "HOME:" in text:
        return text
    headers = tuple(re.finditer(r"^\s*([^\n()]+?)\s*\(\d+-\d+\)\s*$", text, re.MULTILINE))
    if len(headers) != 2 or tuple(_team_name(h.group(1)) for h in headers) != tuple(
        map(_team_name, expected)
    ):
        raise ValueError("Unresolved neutral-site final foul teams")
    for prefix, header in reversed(tuple(zip(("VISITOR:", "HOME:"), headers, strict=True))):
        text = text[: header.start()] + prefix + " " + text[header.start() :].lstrip()
    return text


def _event(value: str, side: int, kind: FoulKind, *, team_only: bool = False) -> FinalFoulEvent:
    """Count flagrant assessments once, preserving the severity separately."""
    if team_only and re.fullmatch(_CLOCK, value):
        value = "Team " + value
    pattern = (
        rf"{_CLOCK}\s+(.+?)-FLG([12])"
        if kind == "flagrant"
        else rf"(.+?)\s+{_CLOCK}(?:\s+\(([^()]+)\))?"
    )
    match = re.fullmatch(pattern, value)
    if match is None:
        raise ValueError("Unsupported final foul event")
    if kind == "flagrant":
        clock, period, name, detail = match.groups()
        detail = "FLG" + detail
    else:
        name, clock, period, detail = match.groups()
    overtime = "OT" in period
    period_number = int(period[0]) + (4 if overtime else 0)
    minutes = int(clock.split(":")[0])
    if minutes > (5 if overtime else 12):
        raise ValueError("Invalid final foul clock")
    return FinalFoulEvent(side, name.strip(), kind, clock, period_number, detail or "")


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())
