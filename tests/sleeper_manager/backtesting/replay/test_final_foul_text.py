"""Synthetic final reports exercise attribution boundaries without private artifacts."""

import pytest

from sleeper_manager.backtesting.replay.final_foul_text import parse_final_foul_text

BOX = """NATIONAL BASKETBALL ASSOCIATION OFFICIAL SCORER'S REPORT FINAL BOX
VISITOR: Example Visitors (1-0)
1 Alex Example    G  30:00  10
HOME: SAMPLE HOSTS (0-1)
2 Bailey Sample   G  30:00  10
SCORE BY PERIOD
Visitors 100 HOSTS 90
{summary}
Copyright (c) 2025-2026 NBA Properties, INC. All Rights Reserved
"""


def test_preserve_players_coaches_team_events_and_double_technicals() -> None:
    """Neither team totals nor unresolved bench names become player counts."""
    parsed = parse_final_foul_text(
        BOX.format(
            summary="""Technical fouls - Individual
Visitors (2): Example 0:01 4th , Coach 0:01 4th
HOSTS (1): Sample 0:01 4th
Technical fouls - Non-Unsportsmanlike
Visitors (1): Team 8:19 4th (Too Many Players)
HOSTS (1): Sample 10:53 2nd (Defensive Three Seconds)
Flagrant Fouls
Visitors (1): 4:17 1st Example-FLG2
HOSTS (0): NONE
Ejections
Visitors (1): Example 4:17 1st
HOSTS (0): NONE"""
        )
    )
    assert parsed.teams == ("Example Visitors", "SAMPLE HOSTS")
    assert [event.name for event in parsed.events] == [
        "Example",
        "Coach",
        "Sample",
        "Team",
        "Sample",
        "Example",
    ]
    assert [event.field for event in parsed.events].count("flagrant_fouls") == 1
    assert parsed.events[-1].detail == "FLG2"
    assert parsed.events[3].detail == "Too Many Players"
    assert len(parsed.roster.rows) == 2


def test_omitted_sections_remain_distinct_from_explicit_zero_summaries() -> None:
    """Parsing a final box alone does not attest to missing foul fields."""
    missing = parse_final_foul_text(BOX.format(summary=""))
    zero = parse_final_foul_text(
        BOX.format(summary="Flagrant Fouls\nVisitors (0): NONE\nHOSTS (0): NONE")
    )
    assert missing.events == zero.events == ()
    assert missing.sections == ()
    assert zero.sections == ("flagrant",)


def test_wrapped_names_repeated_assessments_and_overtime() -> None:
    """Repeated assessments at the same clock retain their multiplicity."""
    parsed = parse_final_foul_text(
        BOX.format(
            summary="""Technical fouls - Individual
Visitors (2): Example Jr. 0:01 1st OT , Example Jr.
0:01 1st OT
HOSTS (0): NONE
MEMO: An unrelated note"""
        )
    )
    assert len(parsed.events) == 2
    assert parsed.events[0] == parsed.events[1]
    assert parsed.events[0].period == 5


@pytest.mark.parametrize(
    "summary",
    [
        "Technical fouls - Unknown\nVisitors (0): NONE\nHOSTS (0): NONE",
        "Flagrant Fouls\nVisitors (0): NONE",
        "Flagrant Fouls\nWrong Team (0): NONE\nHOSTS (0): NONE",
        "Flagrant Fouls\nVisitors (0): Example\nHOSTS (0): NONE",
        "Flagrant Fouls\nVisitors (2): 1:00 1st Example-FLG1\nHOSTS (0): NONE",
        "Flagrant Fouls\nVisitors (1): 13:00 1st Example-FLG1\nHOSTS (0): NONE",
        "Flagrant Fouls\nVisitors (1): 1:00 1st Example-FLG3\nHOSTS (0): NONE",
        "Flagrant Fouls\nVisitors (0): NONE\nHOSTS (0): NONE\n"
        "Flagrant Fouls\nVisitors (0): NONE\nHOSTS (0): NONE",
    ],
)
def test_reject_unexplained_summary_content(summary: str) -> None:
    """Unsupported or contradictory source text cannot silently gain coverage."""
    with pytest.raises(ValueError):
        parse_final_foul_text(BOX.format(summary=summary))


@pytest.mark.parametrize("marker", ["FINAL BOX", "VISITOR:", "HOME:", "SCORE BY", "Copyright"])
def test_reject_truncated_reports(marker: str) -> None:
    """A clipped summary is insufficient evidence for any coverage claim."""
    with pytest.raises(ValueError):
        parse_final_foul_text(BOX.format(summary="").replace(marker, "missing"))
