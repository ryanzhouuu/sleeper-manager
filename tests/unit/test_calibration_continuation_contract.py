"""Verify strict JSON decoding and invariants for calibrated continuation state."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from sleeper_manager.backtesting.artifacts import canonicalize
from sleeper_manager.backtesting.calibration_continuation import (
    CALIBRATED_CONTINUATION_VERSION,
    CalibratedProjectionContinuation,
    PendingCalibrationContinuation,
    decode_calibrated_continuation,
)
from sleeper_manager.backtesting.models import BacktestError


def _continuation() -> CalibratedProjectionContinuation:
    """Return a valid refreshed continuation with one pending target."""
    tipoff = datetime(2025, 11, 5, tzinfo=UTC)
    return CalibratedProjectionContinuation(
        continuation_version=CALIBRATED_CONTINUATION_VERSION,
        model_version="calibrated-fixed-v1-2-3-2",
        wrapped_model_version="fixed-v1",
        min_samples=2,
        max_samples=3,
        refresh_interval=2,
        dataset_version="dataset-v1",
        scoring_policy_version="policy-v1",
        residuals=(1.0, 2.0, 3.0),
        total_residual_count=4,
        last_refresh_count=4,
        sorted_residuals=(1.0, 2.0, 3.0),
        residual_mean=2.0,
        pending=(PendingCalibrationContinuation("player-1", "game-1", tipoff, 20.5),),
        last_evaluated_game_start=tipoff,
    )


def test_continuation_json_round_trip_is_exact_and_strict() -> None:
    """Canonical JSON state round-trips while schema and primitive drift fail."""
    continuation = _continuation()
    payload = canonicalize(continuation)

    assert decode_calibrated_continuation(payload) == continuation
    assert isinstance(payload, dict)
    with pytest.raises(BacktestError, match="fields"):
        decode_calibrated_continuation({**payload, "unknown": True})
    with pytest.raises(BacktestError, match="integer"):
        decode_calibrated_continuation({**payload, "total_residual_count": True})
    with pytest.raises(BacktestError, match="timezone-aware"):
        decode_calibrated_continuation(
            {**payload, "last_evaluated_game_start": "2025-11-05T00:00:00"}
        )


def test_continuation_rejects_nonfinite_and_inconsistent_residual_state() -> None:
    """Persisted residual materializations fail closed when invariants drift."""
    continuation = _continuation()

    with pytest.raises(BacktestError, match="finite"):
        replace(continuation, residual_mean=float("nan"))
    with pytest.raises(BacktestError, match="capacity"):
        replace(continuation, residuals=())
    with pytest.raises(BacktestError, match="refresh"):
        replace(continuation, last_refresh_count=1)


def test_continuation_requires_unique_pending_keys_at_the_current_tipoff() -> None:
    """Pending continuation order is retained without duplicate or stale-tipoff keys."""
    continuation = _continuation()
    pending = continuation.pending[0]

    with pytest.raises(BacktestError, match="unique"):
        replace(continuation, pending=(pending, pending))
    with pytest.raises(BacktestError, match="last evaluated tipoff"):
        replace(
            continuation,
            pending=(replace(pending, game_start=datetime(2025, 11, 4, tzinfo=UTC)),),
        )
