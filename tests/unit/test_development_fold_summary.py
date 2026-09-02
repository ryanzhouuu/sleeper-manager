"""Verify strict aggregate development-summary validation."""

from __future__ import annotations

from copy import deepcopy

import pytest
from checkpoint_support import development_evidence

from sleeper_manager.backtesting.development_fold_summary import (
    validate_development_fold_summary,
)


def test_development_fold_summary_accepts_exact_canonical_evidence() -> None:
    """A live development summary satisfies the persisted schema exactly."""
    _, suite, development, summary = development_evidence()

    validate_development_fold_summary(
        summary,
        fold_name=development.name,
        season_start=development.season_start,
        phase=development.phase,
        model_names=tuple(model.name for model in suite),
    )


def test_development_fold_summary_rejects_schema_and_numeric_drift() -> None:
    """Unknown fields, booleans-as-counts, and model-set drift fail closed."""
    _, suite, development, summary = development_evidence()
    unknown = {**summary, "unknown": True}
    invalid_count = deepcopy(summary)
    invalid_count["target_count"] = True

    for payload in (unknown, invalid_count):
        with pytest.raises(ValueError):
            validate_development_fold_summary(
                payload,
                fold_name=development.name,
                season_start=development.season_start,
                phase=development.phase,
                model_names=tuple(model.name for model in suite),
            )

    with pytest.raises(ValueError, match="model set"):
        validate_development_fold_summary(
            summary,
            fold_name=development.name,
            season_start=development.season_start,
            phase=development.phase,
            model_names=("missing-model",),
        )
