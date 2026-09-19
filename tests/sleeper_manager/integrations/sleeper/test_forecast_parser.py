"""Characterize strict normalization of the qualified Sleeper season feed."""

import gzip
import json
from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.domain.forecast_capture import ForecastSource
from sleeper_manager.integrations.sleeper.forecast_parser import (
    SleeperForecastPayloadError,
    parse_sleeper_season_forecasts,
)
from tests.paths import FIXTURES_DIR

FIXTURE = FIXTURES_DIR / "sleeper" / "season_forecasts.json"
PERSISTED_AT = datetime(2026, 9, 19, 14, tzinfo=UTC)


def source() -> ForecastSource:
    """Build the exact source identity qualified for the parser."""

    return ForecastSource(
        provider="sleeper",
        endpoint="/projections/nba/2026?season_type=regular",
        season="2026",
        season_type="regular",
        horizon="season",
        adapter_version="sleeper-season-forecast-v1",
    )


def fixture_bytes() -> bytes:
    """Load the sanitized source fixture without reserializing its raw bytes."""

    return FIXTURE.read_bytes()


def fixture_rows() -> list[dict[str, object]]:
    """Load mutable fixture rows for semantic-change tests."""

    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(value, list)
    return value


def encoded(rows: list[dict[str, object]], *, indent: int | None = None) -> bytes:
    """Encode test rows with controlled raw formatting."""

    return json.dumps(rows, indent=indent, sort_keys=True).encode("utf-8")


def test_parser_builds_raw_artifact_and_normalized_revision() -> None:
    payload = fixture_bytes()

    parsed = parse_sleeper_season_forecasts(
        payload,
        source=source(),
        persisted_at=PERSISTED_AT,
    )

    assert gzip.decompress(parsed.artifact.encoded_payload) == payload
    assert parsed.artifact.uncompressed_size == len(payload)
    assert parsed.artifact.stored_at == PERSISTED_AT
    assert parsed.revision.source_payload_hash == parsed.artifact.payload_hash
    assert parsed.revision.coverage.total_rows == 3
    assert parsed.revision.coverage.numeric_forecast_rows == 2
    assert parsed.revision.coverage.core_complete_rows == 1
    assert tuple(record.player_id for record in parsed.revision.records) == (
        "1000",
        "2000",
        "3000",
    )
    assert parsed.revision.records[1].stat("tpm") is None
    assert parsed.revision.records[1].stat("pts_std") == 99.0
    assert parsed.revision.records[2].stat("pts") is None
    assert parsed.revision.provider_updated_from is not None
    assert parsed.revision.provider_updated_to is not None
    assert parsed.revision.provider_updated_from < parsed.revision.provider_updated_to


def test_metadata_and_formatting_changes_do_not_create_semantic_revision() -> None:
    rows = fixture_rows()
    baseline = parse_sleeper_season_forecasts(
        encoded(rows), source=source(), persisted_at=PERSISTED_AT
    )
    rows[0]["last_modified"] = 1789739999999
    rows[0]["updated_at"] = 1789739999999
    player_metadata = rows[0]["player"]
    assert isinstance(player_metadata, dict)
    player_metadata["injury_status"] = "Questionable"
    changed = parse_sleeper_season_forecasts(
        encoded(rows, indent=2),
        source=source(),
        persisted_at=PERSISTED_AT + timedelta(minutes=1),
    )

    assert changed.artifact.payload_hash != baseline.artifact.payload_hash
    assert changed.revision.semantic_hash == baseline.revision.semantic_hash
    assert changed.revision.revision_id == baseline.revision.revision_id


@pytest.mark.parametrize("change", ("stats", "team", "presence", "company"))
def test_qualified_source_changes_create_new_semantic_revision(change: str) -> None:
    rows = fixture_rows()
    baseline = parse_sleeper_season_forecasts(
        encoded(rows), source=source(), persisted_at=PERSISTED_AT
    )
    if change == "stats":
        stats = rows[0]["stats"]
        assert isinstance(stats, dict)
        stats["pts"] = 16.0
    elif change == "team":
        rows[0]["team"] = "MIA"
    elif change == "company":
        rows[0]["company"] = "other"
    else:
        rows.pop()

    changed = parse_sleeper_season_forecasts(
        encoded(rows),
        source=source(),
        persisted_at=PERSISTED_AT + timedelta(minutes=1),
    )

    assert changed.revision.semantic_hash != baseline.revision.semantic_hash
    assert changed.revision.revision_id != baseline.revision.revision_id


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (b"{}", "root must be an array"),
        (b"not-json", "valid UTF-8 JSON"),
        (b'[{"player_id":"1","player_id":"2"}]', "duplicated"),
        (b"[NaN]", "constant"),
    ),
)
def test_parser_rejects_ambiguous_json(payload: bytes, message: str) -> None:
    with pytest.raises(SleeperForecastPayloadError, match=message):
        parse_sleeper_season_forecasts(payload, source=source(), persisted_at=PERSISTED_AT)


def test_parser_rejects_missing_fields_and_mismatched_horizon() -> None:
    rows = fixture_rows()
    del rows[0]["stats"]
    with pytest.raises(SleeperForecastPayloadError, match="missing fields: stats"):
        parse_sleeper_season_forecasts(encoded(rows), source=source(), persisted_at=PERSISTED_AT)

    rows = fixture_rows()
    rows[0]["game_id"] = "game-1"
    with pytest.raises(SleeperForecastPayloadError, match="game_id"):
        parse_sleeper_season_forecasts(encoded(rows), source=source(), persisted_at=PERSISTED_AT)


@pytest.mark.parametrize("invalid", (True, "15.7", float("inf")))
def test_parser_rejects_non_numeric_or_non_finite_stats(invalid: object) -> None:
    rows = fixture_rows()
    stats = rows[0]["stats"]
    assert isinstance(stats, dict)
    stats["pts"] = invalid

    with pytest.raises(SleeperForecastPayloadError, match="finite number|constant"):
        parse_sleeper_season_forecasts(encoded(rows), source=source(), persisted_at=PERSISTED_AT)


def test_parser_rejects_duplicate_players_and_wrong_source_binding() -> None:
    rows = fixture_rows()
    rows[1]["player_id"] = rows[0]["player_id"]
    with pytest.raises(SleeperForecastPayloadError, match="duplicate player IDs"):
        parse_sleeper_season_forecasts(encoded(rows), source=source(), persisted_at=PERSISTED_AT)

    wrong_source = ForecastSource(
        provider="other",
        endpoint="/forecasts",
        season="2026",
        season_type="regular",
        horizon="season",
        adapter_version="other-v1",
    )
    with pytest.raises(SleeperForecastPayloadError, match="provider 'sleeper'"):
        parse_sleeper_season_forecasts(
            fixture_bytes(), source=wrong_source, persisted_at=PERSISTED_AT
        )
