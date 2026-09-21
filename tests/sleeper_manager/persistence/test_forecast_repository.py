"""Verify capture-bundle validation before repository transactions begin."""

from dataclasses import replace

import pytest

from sleeper_manager.persistence.forecast_repository import (
    ForecastArchiveError,
    ForecastCaptureWrite,
)
from tests.sleeper_manager.persistence.forecast_sqlite_support import successful_capture


def test_capture_write_rejects_missing_and_unreferenced_objects() -> None:
    """Require attachments exactly when the receipt points to them."""

    capture = successful_capture()
    with pytest.raises(ForecastArchiveError, match="artifact presence"):
        ForecastCaptureWrite(capture.receipt, revision=capture.revision)
    with pytest.raises(ForecastArchiveError, match="revision presence"):
        ForecastCaptureWrite(capture.receipt, artifact=capture.artifact)


def test_capture_write_rejects_broken_raw_and_semantic_hashes() -> None:
    """Verify both content identities before any database mutation."""

    capture = successful_capture()
    assert capture.artifact is not None
    assert capture.revision is not None
    bad_artifact = replace(capture.artifact, payload_hash="a" * 64)
    bad_raw_receipt = replace(capture.receipt, payload_hash="a" * 64)
    with pytest.raises(ForecastArchiveError, match="payload hash does not match"):
        ForecastCaptureWrite(bad_raw_receipt, bad_artifact, capture.revision)

    bad_revision = replace(capture.revision, semantic_hash="b" * 64)
    bad_revision_receipt = replace(capture.receipt, semantic_hash="b" * 64)
    with pytest.raises(ForecastArchiveError, match="semantic hash does not match"):
        ForecastCaptureWrite(bad_revision_receipt, capture.artifact, bad_revision)


def test_capture_write_rejects_cross_capture_links() -> None:
    """Prevent a valid artifact or revision from being attached to another receipt."""

    first = successful_capture()
    second = successful_capture(
        receipt_id="receipt-2",
        payload=b"source-payload-2",
        points=29.0,
    )
    assert first.artifact is not None
    assert first.revision is not None

    with pytest.raises(ForecastArchiveError, match="different raw artifact"):
        ForecastCaptureWrite(second.receipt, first.artifact, second.revision)
    with pytest.raises(ForecastArchiveError, match="different revision"):
        ForecastCaptureWrite(second.receipt, second.artifact, first.revision)
