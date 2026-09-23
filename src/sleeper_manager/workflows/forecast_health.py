"""Summarize forecast archive capture health for an operator.

The report reads receipts and storage measurements. It does not fetch Sleeper
or decide whether a stored forecast is fit for lineup or Lock-In advice.
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from sleeper_manager.domain.forecast_capture import (
    ForecastCaptureOutcome,
    ForecastCaptureTiming,
    ForecastFetchReceipt,
    ForecastSource,
)
from sleeper_manager.persistence.forecast_repository import ForecastArchiveStorage
from sleeper_manager.persistence.forecast_sqlite import SQLiteForecastArchiveRepository
from sleeper_manager.workflows.forecast_schedule import (
    FORECAST_DAILY_CAP,
    FORECAST_REQUEST_OUTCOMES,
)

FORECAST_STORAGE_ALERT_BYTES = 350_000_000

_GAP_OUTCOMES = frozenset(
    {
        ForecastCaptureOutcome.FAILED,
        ForecastCaptureOutcome.INVALID,
        ForecastCaptureOutcome.SUPPRESSED,
    }
)
_CLEAN_OUTCOMES = frozenset(
    {
        ForecastCaptureOutcome.CHANGED,
        ForecastCaptureOutcome.UNCHANGED,
    }
)


@dataclass(frozen=True, slots=True)
class ForecastSourceActivity:
    """One source's requests, writes, and gaps during the reported UTC day."""

    source: ForecastSource
    requests: int
    payload_writes: int
    revision_writes: int
    gaps: tuple[ForecastFetchReceipt, ...]


@dataclass(frozen=True, slots=True)
class ForecastCaptureHealth:
    """Operator view of the newest capture, the current UTC day, and archive size."""

    storage: ForecastArchiveStorage
    latest: ForecastFetchReceipt | None
    activity: tuple[ForecastSourceActivity, ...]
    alert_reasons: tuple[str, ...]

    @property
    def alerting(self) -> bool:
        """Return whether the last capture, today's gaps, or archive size need attention."""

        return bool(self.alert_reasons)


def summarize_forecast_health(
    *,
    storage: ForecastArchiveStorage,
    latest: ForecastFetchReceipt | None,
    today: tuple[ForecastFetchReceipt, ...],
) -> ForecastCaptureHealth:
    """Build a health report. `today` is the UTC day already selected by the caller."""

    reasons: list[str] = []
    if latest is not None and not _clean(latest):
        reasons.append("last capture is not a clean success")
    if any(_gap(receipt) for receipt in today):
        reasons.append("capture gaps persisted today")
    encoded = storage.raw_encoded_bytes + storage.revision_encoded_bytes
    if encoded >= FORECAST_STORAGE_ALERT_BYTES:
        reasons.append("encoded payload bytes reached 350 MB")
    return ForecastCaptureHealth(
        storage=storage,
        latest=latest,
        activity=_activity_by_source(today),
        alert_reasons=tuple(reasons),
    )


def read_forecast_capture_health(path: Path, *, now: datetime) -> ForecastCaptureHealth:
    """Read one local archive. A missing file is an empty report and is not created."""

    if not path.exists():
        return summarize_forecast_health(
            storage=ForecastArchiveStorage(
                artifact_count=0,
                revision_count=0,
                receipt_count=0,
                raw_encoded_bytes=0,
                raw_uncompressed_bytes=0,
                revision_encoded_bytes=0,
                revision_uncompressed_bytes=0,
            ),
            latest=None,
            today=(),
        )
    start, end = _utc_day(now)
    archive = SQLiteForecastArchiveRepository(path)
    return summarize_forecast_health(
        storage=archive.measure_storage(),
        latest=archive.load_newest_receipt(),
        today=archive.list_receipts_between(start=start, end=end),
    )


def run_forecast_capture_health(path: Path, *, state_backend: str, now: datetime) -> int:
    """Print archive health and return 0, 1, or 2. A missing archive is not created."""

    if state_backend != "sqlite":
        print("Forecast capture health requires STATE_BACKEND=sqlite", file=sys.stderr)
        return 2
    try:
        report = read_forecast_capture_health(path, now=now)
    except (OSError, sqlite3.Error) as error:
        print(f"Forecast capture health failed: {error}", file=sys.stderr)
        return 2
    print(render_forecast_health(report, path=path))
    return 1 if report.alerting else 0


def render_forecast_health(report: ForecastCaptureHealth, *, path: Path) -> str:
    """Format one health report as operator text. `path` is the archive being described."""

    lines = [f"Forecast archive: {path}", _latest_line(report.latest)]
    if report.latest is not None and report.latest.error_code is not None:
        lines.append(f"Last capture error: {report.latest.error_code}")
    lines.extend(_activity_lines(report.activity))
    encoded = report.storage.raw_encoded_bytes + report.storage.revision_encoded_bytes
    lines.append(
        "Storage: "
        f"{report.storage.artifact_count} artifacts, "
        f"{report.storage.revision_count} revisions, "
        f"{report.storage.receipt_count} receipts, "
        f"{encoded} encoded bytes (alert at {FORECAST_STORAGE_ALERT_BYTES})"
    )
    if report.alert_reasons:
        lines.append("Alert: " + "; ".join(report.alert_reasons))
    return "\n".join(lines)


def _activity_by_source(
    today: tuple[ForecastFetchReceipt, ...],
) -> tuple[ForecastSourceActivity, ...]:
    """Group one UTC day by source, preserving receipt order inside each source."""

    grouped: dict[ForecastSource, list[ForecastFetchReceipt]] = {}
    for receipt in today:
        grouped.setdefault(receipt.source, []).append(receipt)
    return tuple(
        _activity(source, tuple(grouped[source])) for source in sorted(grouped, key=_source_order)
    )


def _activity(
    source: ForecastSource,
    receipts: tuple[ForecastFetchReceipt, ...],
) -> ForecastSourceActivity:
    """Count requests and writes, and keep every gap receipt for that source."""

    return ForecastSourceActivity(
        source=source,
        requests=sum(receipt.outcome in FORECAST_REQUEST_OUTCOMES for receipt in receipts),
        payload_writes=sum(receipt.payload_hash is not None for receipt in receipts),
        revision_writes=sum(
            receipt.outcome is ForecastCaptureOutcome.CHANGED for receipt in receipts
        ),
        gaps=tuple(receipt for receipt in receipts if _gap(receipt)),
    )


def _activity_lines(activity: tuple[ForecastSourceActivity, ...]) -> tuple[str, ...]:
    """Render per-source request, write, and gap lines, or the empty-day lines."""

    if not activity:
        return ("Requests today: none", "Writes today: none", "Gaps today: none")
    lines = ["Requests today:"]
    lines.extend(
        f"  {item.source.season} {item.source.season_type}: {item.requests}/{FORECAST_DAILY_CAP}"
        for item in activity
    )
    lines.append("Writes today:")
    lines.extend(
        (
            f"  {item.source.season} {item.source.season_type}: "
            f"{item.payload_writes} payloads, {item.revision_writes} revisions"
        )
        for item in activity
    )
    gaps = [receipt for item in activity for receipt in item.gaps]
    if not gaps:
        lines.append("Gaps today: none")
        return tuple(lines)
    lines.append("Gaps today:")
    lines.extend(_gap_line(receipt) for receipt in gaps)
    return tuple(lines)


def _latest_line(latest: ForecastFetchReceipt | None) -> str:
    """Describe the newest receipt, including when the archive has none."""

    if latest is None:
        return "Last capture: none"
    return (
        "Last capture: "
        f"{latest.outcome.value} {latest.timing.value} "
        f"at {latest.persisted_at.isoformat()}"
    )


def _gap_line(receipt: ForecastFetchReceipt) -> str:
    """Describe one gap, including its error code when the attempt recorded one."""

    line = f"  {receipt.outcome.value} {receipt.timing.value} at {receipt.persisted_at.isoformat()}"
    if receipt.error_code is not None:
        line += f" error={receipt.error_code}"
    return line


def _utc_day(now: datetime) -> tuple[datetime, datetime]:
    """Return the half-open UTC day that contains an aware clock reading."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Forecast health clock must be timezone-aware")
    utc_now = now.astimezone(UTC)
    start = datetime.combine(utc_now.date(), time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def _gap(receipt: ForecastFetchReceipt) -> bool:
    """Return whether this attempt failed, was rejected, was skipped, or finished late."""

    return receipt.outcome in _GAP_OUTCOMES or receipt.timing is ForecastCaptureTiming.LATE


def _clean(receipt: ForecastFetchReceipt) -> bool:
    """Return whether this attempt stored an on-time changed or unchanged forecast."""

    return receipt.outcome in _CLEAN_OUTCOMES and receipt.timing is ForecastCaptureTiming.ON_TIME


def _source_order(source: ForecastSource) -> tuple[str, ...]:
    """Order source groups stably when more than one feed was captured."""

    return (
        source.provider,
        source.season,
        source.season_type,
        source.horizon,
        source.endpoint,
        source.adapter_version,
    )


__all__ = (
    "FORECAST_STORAGE_ALERT_BYTES",
    "ForecastCaptureHealth",
    "ForecastSourceActivity",
    "read_forecast_capture_health",
    "render_forecast_health",
    "run_forecast_capture_health",
    "summarize_forecast_health",
)
