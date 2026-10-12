"""Extract explicit roster rows from retained official NBA final-report layout text.

Names remain unresolved: callers must independently establish unique provider
identities and team associations before constructing a reviewed census ledger.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FinalRosterRow:
    """Retain an explicit named row and visitor (0) or home (1) side."""

    name: str
    side: int
    did_play: bool


@dataclass(frozen=True, slots=True)
class FinalRosterText:
    """Keep full box-row names separate from unresolved inactive surnames."""

    rows: tuple[FinalRosterRow, ...]
    inactive_names: tuple[tuple[str, ...], tuple[str, ...]]


def parse_final_roster_text(text: str) -> FinalRosterText:
    """Parse positive named evidence; never infer an outcome from a missing row."""
    if not all(marker in text for marker in ("FINAL BOX", "VISITOR:", "HOME:", "SCORE BY")):
        raise ValueError("Unsupported final roster report")
    visitor, home = text.split("HOME:", 1)
    sections = (visitor.split("VISITOR:", 1)[1], home.split("SCORE BY", 1)[0])
    rows = []
    for side, section in enumerate(sections):
        for line in section.splitlines():
            match = re.match(
                r"^\s*\d+\s+(.+?)\s{2,}(?:[FCG]\s+)?(\d{1,2}:\d{2}|DNP|DND|NWT)\b",
                line,
            )
            if match is None:
                continue
            name, status = match.groups()
            played = bool(re.fullmatch(r"\d{1,2}:\d{2}", status))
            rows.append(FinalRosterRow(name.strip(), side, played))
    blocks = re.findall(
        r"Inactive:\s*[^-\n]+-\s*(.*?)(?=\n\s*Inactive:|\n\s*Points in the Paint:)",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    names: list[tuple[str, ...]] = []
    for block in blocks if len(blocks) == 2 else ():
        entries = re.findall(r"(?:^|(?<=\),))\s*([^()]+?)\s*\(([^()]*)\)", block)
        names.append(
            tuple(
                " ".join(name.split())
                for name, reason in entries
                if "trade pending" not in reason.casefold()
            )
        )
    while len(names) < 2:
        names.append(())
    return FinalRosterText(tuple(rows), (names[0], names[1]))
