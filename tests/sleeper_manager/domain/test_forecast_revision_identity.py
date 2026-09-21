"""Verify the provider-neutral identity of normalized forecast revisions."""

from datetime import UTC, datetime, timedelta, timezone

from sleeper_manager.domain.forecast_capture import ForecastSource, NormalizedPlayerForecast
from sleeper_manager.domain.forecast_revision_identity import forecast_semantic_hash


def source() -> ForecastSource:
    """Build one stable forecast source identity."""

    return ForecastSource("sleeper", "/forecasts", "2026", "regular", "season", "v1")


def record(
    player_id: str,
    *,
    points: float = 20.0,
    updated_at: datetime | None = None,
) -> NormalizedPlayerForecast:
    """Build one normalized record with controllable semantic and metadata fields."""

    return NormalizedPlayerForecast(
        player_id=player_id,
        company="sleeper",
        team_id="CHI",
        stats=(("pts", points), ("ast", 5.0)),
        provider_updated_at=updated_at,
    )


def test_semantic_hash_ignores_order_and_provider_timestamps() -> None:
    """Keep metadata-only source changes within one normalized revision."""

    initial = (
        record("2000"),
        record("1000", updated_at=datetime(2026, 9, 21, tzinfo=UTC)),
    )
    metadata_change = (
        record("1000", updated_at=datetime(2026, 9, 22, tzinfo=UTC)),
        record("2000", updated_at=datetime(2026, 9, 21, tzinfo=UTC)),
    )

    assert forecast_semantic_hash(source(), initial) == forecast_semantic_hash(
        source(), metadata_change
    )


def test_semantic_hash_tracks_source_presence_and_forecast_values() -> None:
    """Change identity when a decision-relevant source or record field changes."""

    baseline = forecast_semantic_hash(source(), (record("1000"), record("2000")))
    changed_source = ForecastSource(
        "sleeper",
        "/forecasts",
        "2026",
        "regular",
        "season",
        "v2",
    )

    assert forecast_semantic_hash(changed_source, (record("1000"), record("2000"))) != baseline
    assert forecast_semantic_hash(source(), (record("1000"),)) != baseline
    assert (
        forecast_semantic_hash(
            source(),
            (record("1000", points=21.0), record("2000")),
        )
        != baseline
    )


def test_semantic_hash_normalizes_equivalent_timezones_out_of_identity() -> None:
    """Confirm timestamps remain irrelevant even when represented with another offset."""

    utc = record("1000", updated_at=datetime(2026, 9, 21, 12, tzinfo=UTC))
    central = record(
        "1000",
        updated_at=datetime(2026, 9, 21, 7, tzinfo=timezone(-timedelta(hours=5))),
    )

    assert forecast_semantic_hash(source(), (utc,)) == forecast_semantic_hash(source(), (central,))
