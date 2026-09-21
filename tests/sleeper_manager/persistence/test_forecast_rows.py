"""Verify shared forecast archive row encoders and decoders."""

from sleeper_manager.persistence.forecast_repository import ForecastArchiveIntegrityError
from sleeper_manager.persistence.forecast_rows import (
    artifact_from_mapping,
    artifact_insert_params,
    receipt_from_mapping,
    receipt_insert_params,
    revision_from_mapping,
    revision_insert_params,
)
from tests.sleeper_manager.persistence.forecast_sqlite_support import successful_capture

ARTIFACT_COLUMNS = (
    "payload_hash",
    "encoding",
    "encoded_payload",
    "uncompressed_size",
    "stored_at",
)
REVISION_COLUMNS = (
    "revision_id",
    "provider",
    "endpoint",
    "season",
    "season_type",
    "horizon",
    "adapter_version",
    "semantic_hash",
    "source_payload_hash",
    "first_persisted_at",
    "provider_updated_from",
    "provider_updated_to",
    "total_rows",
    "numeric_forecast_rows",
    "core_complete_rows",
    "records_encoding",
    "encoded_records",
    "records_uncompressed_size",
)
RECEIPT_COLUMNS = (
    "receipt_id",
    "provider",
    "endpoint",
    "season",
    "season_type",
    "horizon",
    "adapter_version",
    "scheduled_for",
    "started_at",
    "response_received_at",
    "persisted_at",
    "outcome",
    "timing",
    "http_status",
    "payload_hash",
    "semantic_hash",
    "revision_id",
    "error_code",
)


def test_row_converters_round_trip_complete_capture() -> None:
    """Preserve domain values through the backend-neutral row boundary."""

    capture = successful_capture()
    assert capture.artifact is not None
    assert capture.revision is not None

    artifact_row = dict(
        zip(ARTIFACT_COLUMNS, artifact_insert_params(capture.artifact), strict=True)
    )
    revision_row = dict(
        zip(REVISION_COLUMNS, revision_insert_params(capture.revision), strict=True)
    )
    receipt_row = dict(zip(RECEIPT_COLUMNS, receipt_insert_params(capture.receipt), strict=True))

    assert artifact_from_mapping(artifact_row) == capture.artifact
    assert revision_from_mapping(revision_row) == capture.revision
    assert receipt_from_mapping(receipt_row) == capture.receipt


def test_revision_decoder_rejects_mismatched_semantic_identity() -> None:
    """Reject stored records that no longer match their semantic hash."""

    capture = successful_capture()
    assert capture.revision is not None
    row = dict(zip(REVISION_COLUMNS, revision_insert_params(capture.revision), strict=True))
    row["semantic_hash"] = "f" * 64

    try:
        revision_from_mapping(row)
    except ForecastArchiveIntegrityError as error:
        assert "semantic hash" in str(error)
    else:
        raise AssertionError("Expected corrupt forecast revision row to be rejected")


def test_row_decoders_accept_d1_blob_arrays() -> None:
    """Convert D1's returned byte arrays at the shared row boundary."""

    capture = successful_capture()
    assert capture.artifact is not None
    assert capture.revision is not None
    artifact_row = dict(
        zip(ARTIFACT_COLUMNS, artifact_insert_params(capture.artifact), strict=True)
    )
    revision_row = dict(
        zip(REVISION_COLUMNS, revision_insert_params(capture.revision), strict=True)
    )
    artifact_row["encoded_payload"] = list(capture.artifact.encoded_payload)
    revision_row["encoded_records"] = list(revision_row["encoded_records"])

    assert artifact_from_mapping(artifact_row) == capture.artifact
    assert revision_from_mapping(revision_row) == capture.revision


def test_row_decoders_reject_invalid_d1_blob_arrays() -> None:
    """Reject non-byte values instead of silently coercing malformed D1 rows."""

    capture = successful_capture()
    assert capture.artifact is not None
    artifact_row = dict(
        zip(ARTIFACT_COLUMNS, artifact_insert_params(capture.artifact), strict=True)
    )
    artifact_row["encoded_payload"] = [31, True, 300]

    try:
        artifact_from_mapping(artifact_row)
    except ForecastArchiveIntegrityError as error:
        assert "payload must be bytes" in str(error)
    else:
        raise AssertionError("Expected malformed D1 byte array to be rejected")
