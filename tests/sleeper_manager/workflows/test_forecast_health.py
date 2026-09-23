"""Verify forecast capture health counts, gap alerts, and operator text."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from sleeper_manager.domain.forecast_capture import (
    ForecastCaptureOutcome,
    ForecastCaptureTiming,
    ForecastFetchReceipt,
)
from sleeper_manager.persistence.forecast_repository import ForecastArchiveStorage
from sleeper_manager.persistence.forecast_sqlite import SQLiteForecastArchiveRepository
from sleeper_manager.workflows.forecast_health import (
    FORECAST_STORAGE_ALERT_BYTES,
    ForecastCaptureHealth,
    read_forecast_capture_health,
    render_forecast_health,
    summarize_forecast_health,
)
from tests.sleeper_manager.persistence.forecast_sqlite_support import (
    BASE,
    SOURCE,
    failed_capture,
    successful_capture,
    suppressed_capture,
)

OTHER = replace(SOURCE, season="2027")
PATH = Path(".local/forecasts.db")


def _storage(**overrides: int) -> ForecastArchiveStorage:
    """Build storage metrics, overriding only the counts under test."""

    values = {
        "artifact_count": 0,
        "revision_count": 0,
        "receipt_count": 0,
        "raw_encoded_bytes": 0,
        "raw_uncompressed_bytes": 0,
        "revision_encoded_bytes": 0,
        "revision_uncompressed_bytes": 0,
    }
    values.update(overrides)
    return ForecastArchiveStorage(**values)


def _summarize(
    latest: ForecastFetchReceipt | None = None,
    today: tuple[ForecastFetchReceipt, ...] = (),
    **storage: int,
) -> ForecastCaptureHealth:
    """Summarize one report from explicit receipts and storage."""

    return summarize_forecast_health(
        storage=_storage(**storage),
        latest=latest,
        today=today,
    )


def test_read_limits_gaps_to_the_clock_utc_day(tmp_path: Path) -> None:
    """The newest receipt can be older than the UTC day used for gap and request counts."""

    path = tmp_path / "forecasts.db"
    archive = SQLiteForecastArchiveRepository(path)
    archive.initialize()
    yesterday = failed_capture(persisted_at=BASE - timedelta(days=1))
    today = successful_capture()
    archive.save_capture(yesterday)
    archive.save_capture(today)

    report = read_forecast_capture_health(path, now=BASE + timedelta(hours=2))
    missing = tmp_path / "missing.db"
    empty = read_forecast_capture_health(missing, now=BASE)

    assert report.latest == today.receipt
    assert report.alerting is False
    assert report.activity[0].gaps == ()
    assert empty.latest is None
    assert not missing.exists()


def test_empty_archive_is_quiet() -> None:
    """A missing history is not an alert."""

    report = _summarize()

    assert report.alerting is False
    assert report.alert_reasons == ()
    assert report.activity == ()
    text = render_forecast_health(report, path=PATH)
    assert "Forecast archive: .local/forecasts.db" in text
    assert "Last capture: none" in text
    assert "Requests today: none" in text
    assert "Gaps today: none" in text
    assert f"0 encoded bytes (alert at {FORECAST_STORAGE_ALERT_BYTES})" in text
    assert "Alert:" not in text


def test_clean_capture_counts_one_request_and_revision_write() -> None:
    """An on-time changed receipt is a request, a payload write, and a revision write."""

    changed = successful_capture().receipt
    report = _summarize(changed, (changed,), artifact_count=1, revision_count=1, receipt_count=1)

    assert report.alerting is False
    assert len(report.activity) == 1
    activity = report.activity[0]
    assert activity.source == SOURCE
    assert (activity.requests, activity.payload_writes, activity.revision_writes) == (1, 1, 1)
    assert activity.gaps == ()
    text = render_forecast_health(report, path=PATH)
    assert "Last capture: changed on_time" in text
    assert "2026 regular: 1/5" in text
    assert "2026 regular: 1 payloads, 1 revisions" in text


def test_failed_invalid_suppressed_and_late_receipts_are_gaps() -> None:
    """Failures, invalid payloads, skipped requests, and late attempts stay visible."""

    changed = successful_capture(receipt_id="changed").receipt
    failed = failed_capture(receipt_id="failed", persisted_at=BASE + timedelta(minutes=10)).receipt
    invalid = replace(
        changed,
        receipt_id="invalid",
        persisted_at=BASE + timedelta(minutes=20),
        scheduled_for=BASE + timedelta(minutes=15),
        started_at=BASE + timedelta(minutes=16),
        response_received_at=BASE + timedelta(minutes=17),
        outcome=ForecastCaptureOutcome.INVALID,
        semantic_hash=None,
        revision_id=None,
        error_code="invalid_payload",
    )
    suppressed = suppressed_capture(
        receipt_id="suppressed",
        persisted_at=BASE + timedelta(minutes=30),
    ).receipt
    late = replace(changed, receipt_id="late", timing=ForecastCaptureTiming.LATE)
    today = (late, failed, invalid, suppressed)
    report = _summarize(suppressed, today)

    assert report.alerting is True
    assert report.alert_reasons == (
        "last capture is not a clean success",
        "capture gaps persisted today",
    )
    activity = report.activity[0]
    assert activity.gaps == today
    assert (activity.requests, activity.payload_writes, activity.revision_writes) == (3, 2, 1)
    text = render_forecast_health(report, path=PATH)
    assert "Gaps today:" in text
    assert "failed on_time" in text
    assert "invalid on_time" in text
    assert "suppressed on_time" in text
    assert "changed late" in text
    assert "error=daily_cap" in text


def test_request_counts_stay_separate_for_each_source() -> None:
    """Two season feeds do not share one five-request cap."""

    first = successful_capture(receipt_id="first").receipt
    second = replace(
        successful_capture(receipt_id="second", persisted_at=BASE + timedelta(hours=1)).receipt,
        source=OTHER,
    )
    report = _summarize(second, (first, second))

    assert [activity.source for activity in report.activity] == [SOURCE, OTHER]
    assert [activity.requests for activity in report.activity] == [1, 1]
    text = render_forecast_health(report, path=PATH)
    assert "2026 regular: 1/5" in text
    assert "2027 regular: 1/5" in text


def test_older_gap_does_not_alert_after_a_later_clean_day() -> None:
    """Yesterday's failure remains stored, and a later clean capture clears the alert."""

    failed = failed_capture(persisted_at=BASE - timedelta(days=1)).receipt
    changed = successful_capture().receipt
    report = _summarize(changed, (changed,))

    assert report.latest == changed
    assert report.alerting is False
    assert report.activity[0].gaps == ()
    stale = _summarize(failed, ())
    assert stale.alerting is True
    assert stale.alert_reasons == ("last capture is not a clean success",)
    assert stale.activity == ()


def test_encoded_payload_bytes_alert_at_350_mb() -> None:
    """Alert on the summed encoded payload size, including a total split across tables."""

    changed = successful_capture().receipt
    below = _summarize(changed, (changed,), raw_encoded_bytes=FORECAST_STORAGE_ALERT_BYTES - 1)
    exact = _summarize(changed, (changed,), raw_encoded_bytes=FORECAST_STORAGE_ALERT_BYTES)
    split = _summarize(
        changed,
        (changed,),
        raw_encoded_bytes=200_000_000,
        revision_encoded_bytes=150_000_000,
    )

    assert below.alerting is False
    assert exact.alert_reasons == ("encoded payload bytes reached 350 MB",)
    assert split.alert_reasons == ("encoded payload bytes reached 350 MB",)
    assert f"{FORECAST_STORAGE_ALERT_BYTES} encoded bytes" in render_forecast_health(
        exact, path=PATH
    )
