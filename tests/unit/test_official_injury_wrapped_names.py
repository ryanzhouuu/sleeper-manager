"""Regression coverage for surnames split across PDF text-extraction tokens."""

from datetime import UTC, datetime

import pytest

from sleeper_manager.domain.nba import AvailabilityStatus, SourceMetadata
from sleeper_manager.integrations.nba.official_injury_parser import (
    parse_official_injury_report_text,
)


@pytest.mark.parametrize("surname", ["Gilgeous-\nAlexander,", "Gilgeous-Alexander,"])
def test_wrapped_surname_does_not_pollute_prior_reason(surname: str) -> None:
    text = (
        "Injury\nReport:\n02/04/26\n09:30\nPM\n"
        "02/04/2026\n09:30\n(ET)\nOKC@SAS\nOklahoma\nCity\nThunder\n"
        "Dort,\nLuguentz\nOut\nInjury/Illness\n-\nRight\nKnee;\nInflammation\n"
        f"{surname}\nShai\nOut\nInjury/Illness\n-\nAbdominal;\nStrain\n"
        "Williams,\nJalen\nOut\nInjury/Illness\n-\nRight\nHamstring;\nStrain\n"
    )
    source = SourceMetadata("fixture", "wrapped-name", datetime(2026, 2, 5, 3, tzinfo=UTC))

    report = parse_official_injury_report_text(text, source=source)

    assert [entry.player_name for entry in report.entries] == [
        "Dort, Luguentz",
        "Gilgeous-Alexander, Shai",
        "Williams, Jalen",
    ]
    assert report.entries[0].reason == "Injury/Illness - Right Knee; Inflammation"
    assert report.entries[1].status is AvailabilityStatus.OUT
    assert report.entries[1].reason == "Injury/Illness - Abdominal; Strain"
