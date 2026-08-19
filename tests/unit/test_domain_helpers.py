from datetime import UTC, date, datetime

import pytest

from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.domain.statistics import weighted_mean


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (date(2025, 9, 30), 2024),
        (date(2025, 10, 1), 2025),
        (datetime(2025, 10, 1, tzinfo=UTC), 2025),
    ),
)
def test_nba_season_start_year_uses_october_boundary(value: date, expected: int) -> None:
    assert nba_season_start_year(value) == expected


def test_weighted_mean_calculates_weighted_values() -> None:
    assert weighted_mean(((10.0, 1.0), (20.0, 3.0))) == 17.5


def test_weighted_mean_rejects_empty_or_zero_weight_observations() -> None:
    with pytest.raises(ValueError):
        weighted_mean(())
    with pytest.raises(ValueError):
        weighted_mean(((10.0, 1.0), (20.0, -1.0)))
