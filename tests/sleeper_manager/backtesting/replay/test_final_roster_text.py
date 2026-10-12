"""Keep positive participation evidence separate from absent or ambiguous names."""

import pytest

from sleeper_manager.backtesting.replay.final_roster_text import parse_final_roster_text

TEXT = """OFFICIAL SCORER'S REPORT
FINAL BOX
VISITOR: Away Team
1 Alice Player     F  30:10  1 2 3
2 Bob Bench           DNP - Coach's Decision
HOME: Home Team
3 Carol Center     C  00:00  0 0 0
4 Dave Rest            NWT - Rest
SCORE BY PERIOD
Inactive: Away - Unique (Injury/Illness - Left Foot; Pain), Other (G League - Two-Way)
Inactive: Home - Smith (Injury/Illness - Back; Pain)
Points in the Paint: Away 10, Home 20
"""


def test_extracts_explicit_named_rows_and_inactive_entries() -> None:
    parsed = parse_final_roster_text(TEXT)
    assert [(r.name, r.side, r.did_play) for r in parsed.rows] == [
        ("Alice Player", 0, True),
        ("Bob Bench", 0, False),
        ("Carol Center", 1, True),
        ("Dave Rest", 1, False),
    ]
    assert parsed.inactive_names == (("Unique", "Other"), ("Smith",))


def test_trade_pending_and_unparsed_rows_do_not_become_dnp_evidence() -> None:
    text = TEXT.replace("Unique (Injury/Illness - Left Foot; Pain)", "Unique (Trade Pending)")
    text = text.replace(
        "2 Bob Bench           DNP - Coach's Decision", "2 Bob Bench           UNKNOWN"
    )
    parsed = parse_final_roster_text(text)
    assert "Unique" not in parsed.inactive_names[0]
    assert "Bob Bench" not in {row.name for row in parsed.rows}


def test_a_single_inactive_block_does_not_invent_its_team_side() -> None:
    text = TEXT.replace("Inactive: Home - Smith (Injury/Illness - Back; Pain)\n", "")
    assert parse_final_roster_text(text).inactive_names == ((), ())


def test_report_without_period_label_preserves_named_rows() -> None:
    assert parse_final_roster_text(
        TEXT.replace("SCORE BY PERIOD", "SCORE BY")
    ) == parse_final_roster_text(TEXT)


@pytest.mark.parametrize("removed", ("FINAL BOX", "VISITOR:", "HOME:", "SCORE BY PERIOD"))
def test_incomplete_report_layout_is_rejected(removed: str) -> None:
    with pytest.raises(ValueError, match="Unsupported final roster report"):
        parse_final_roster_text(TEXT.replace(removed, ""))
