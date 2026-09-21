"""Verify deterministic storage and strict recovery of normalized forecast snapshots."""

import gzip
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from sleeper_manager.domain.forecast_capture import (
    ForecastArtifactEncoding,
    NormalizedPlayerForecast,
)
from sleeper_manager.persistence.forecast_codec import (
    FORECAST_SNAPSHOT_SCHEMA_VERSION,
    EncodedForecastSnapshot,
    ForecastSnapshotCodecError,
    decode_forecast_snapshot,
    encode_forecast_snapshot,
)


def player(
    player_id: str,
    *,
    updated_at: datetime | None = None,
) -> NormalizedPlayerForecast:
    """Build one normalized player record with deliberately unsorted input stats."""

    return NormalizedPlayerForecast(
        player_id=player_id,
        company="sleeper",
        team_id="CHI",
        stats=(("pts", 21.5), ("ast", 5.25)),
        provider_updated_at=updated_at,
    )


def encoded_json(text: str) -> EncodedForecastSnapshot:
    """Wrap controlled JSON in the production compression envelope."""

    payload = text.encode("utf-8")
    return EncodedForecastSnapshot(
        encoding=ForecastArtifactEncoding.GZIP_JSON,
        encoded_records=gzip.compress(payload, compresslevel=6, mtime=0),
        uncompressed_size=len(payload),
    )


def test_snapshot_encoding_is_canonical_and_deterministic() -> None:
    """Produce identical bytes regardless of record order or wall-clock time."""

    timestamp = datetime(2026, 9, 21, 12, tzinfo=timezone(timedelta(hours=-5)))
    first = encode_forecast_snapshot((player("2000"), player("1000", updated_at=timestamp)))
    second = encode_forecast_snapshot((player("1000", updated_at=timestamp), player("2000")))

    assert first == second
    assert first.encoding is ForecastArtifactEncoding.GZIP_JSON
    assert gzip.decompress(first.encoded_records) == (
        b'{"records":[{"company":"sleeper","player_id":"1000",'
        b'"provider_updated_at":"2026-09-21T17:00:00Z","stats":{"ast":5.25,'
        b'"pts":21.5},"team_id":"CHI"},{"company":"sleeper","player_id":"2000",'
        b'"provider_updated_at":null,"stats":{"ast":5.25,"pts":21.5},'
        b'"team_id":"CHI"}],"schema_version":"normalized-forecast-records-v1"}'
    )
    assert first.uncompressed_size == len(gzip.decompress(first.encoded_records))


def test_snapshot_round_trip_reconstructs_domain_records() -> None:
    """Decode stored bytes through the same domain validation used at capture time."""

    records = (player("2000"), player("1000", updated_at=datetime(2026, 9, 21, tzinfo=UTC)))

    decoded = decode_forecast_snapshot(encode_forecast_snapshot(records))

    assert decoded == tuple(sorted(records, key=lambda item: item.player_id))


def test_snapshot_codec_supports_an_empty_source_surface() -> None:
    """Represent a valid capture containing no provider rows without special casing."""

    encoded = encode_forecast_snapshot(())

    assert decode_forecast_snapshot(encoded) == ()
    assert json.loads(gzip.decompress(encoded.encoded_records)) == {
        "records": [],
        "schema_version": FORECAST_SNAPSHOT_SCHEMA_VERSION,
    }


@pytest.mark.parametrize(
    ("snapshot", "message"),
    (
        (
            EncodedForecastSnapshot(
                ForecastArtifactEncoding.GZIP_JSON,
                b"not-gzip",
                8,
            ),
            "valid gzip",
        ),
        (
            EncodedForecastSnapshot(
                ForecastArtifactEncoding.GZIP_JSON,
                gzip.compress(b"{}", mtime=0),
                99,
            ),
            "size",
        ),
        (encoded_json("not-json"), "valid UTF-8 JSON"),
        (encoded_json('{"schema_version":"future-v2","records":[]}'), "schema version"),
        (
            encoded_json(
                '{"schema_version":"normalized-forecast-records-v1","records":[],"records":[]}'
            ),
            "duplicate field",
        ),
        (
            encoded_json(
                '{"schema_version":"normalized-forecast-records-v1","records":[],"extra":true}'
            ),
            "fields",
        ),
    ),
)
def test_snapshot_decoder_rejects_corrupt_or_incompatible_envelopes(
    snapshot: EncodedForecastSnapshot,
    message: str,
) -> None:
    """Fail closed when storage bytes cannot represent the current snapshot contract."""

    with pytest.raises(ForecastSnapshotCodecError, match=message):
        decode_forecast_snapshot(snapshot)


@pytest.mark.parametrize(
    ("record_json", "message"),
    (
        (
            '{"player_id":"1000","company":"sleeper","team_id":null,'
            '"provider_updated_at":null,"stats":{"pts":true}}',
            "finite number",
        ),
        (
            '{"player_id":"1000","company":"sleeper","team_id":null,'
            '"provider_updated_at":"2026-09-21T12:00:00+00:00","stats":{}}',
            "UTC timestamp",
        ),
        (
            '{"player_id":"1000","company":"sleeper","team_id":null,'
            '"provider_updated_at":null,"stats":{},"extra":true}',
            "fields",
        ),
    ),
)
def test_snapshot_decoder_rejects_invalid_player_records(
    record_json: str,
    message: str,
) -> None:
    """Reject records that bypass normalized player invariants or the versioned shape."""

    snapshot = encoded_json(
        f'{{"schema_version":"normalized-forecast-records-v1","records":[{record_json}]}}'
    )

    with pytest.raises(ForecastSnapshotCodecError, match=message):
        decode_forecast_snapshot(snapshot)


def test_snapshot_decoder_rejects_duplicate_player_ids() -> None:
    """Keep revision presence unambiguous after decoding persisted bytes."""

    record = (
        '{"player_id":"1000","company":"sleeper","team_id":null,'
        '"provider_updated_at":null,"stats":{}}'
    )
    snapshot = encoded_json(
        f'{{"schema_version":"normalized-forecast-records-v1","records":[{record},{record}]}}'
    )

    with pytest.raises(ForecastSnapshotCodecError, match="duplicate player"):
        decode_forecast_snapshot(snapshot)
