"""Joint feasibility, concentration, and the minimum-departure weighting contract."""

from dataclasses import replace
from math import fsum

import pytest

from sleeper_manager.projections.hybrid_config import HybridProjectionConfig, HybridProjectionError
from sleeper_manager.projections.hybrid_fitting import fit_joint_weights, weight_support

CONFIG = HybridProjectionConfig()
VECTORS = tuple((float(i), float(i * 2), 0.0, 0.0, 0.0, 0.0, 0.0) for i in range(1, 41))
BASE = (1 / 40,) * 40


def test_feasible_base_weights_are_unchanged() -> None:
    result = fit_joint_weights(VECTORS, BASE, (20.5, 41.0, 0, 0, 0, 0, 0), CONFIG)
    assert result.weights == BASE
    assert result.iterations == 0
    assert result.failure is None


def test_joint_reweighting_moves_means_without_changing_stat_lines() -> None:
    result = fit_joint_weights(VECTORS, BASE, (30.0, 60.0, 0, 0, 0, 0, 0), CONFIG)
    assert result.failure is None
    assert fsum(result.weights) == pytest.approx(1)
    mean = fsum(row[0] * w for row, w in zip(VECTORS, result.weights, strict=True))
    assert mean == pytest.approx(27.0, abs=1e-6)
    assert weight_support(result.weights, CONFIG) is None
    assert result == fit_joint_weights(VECTORS, BASE, (30, 60, 0, 0, 0, 0, 0), CONFIG)


def test_individually_in_range_but_jointly_impossible_means_fail() -> None:
    result = fit_joint_weights(VECTORS, BASE, (5.0, 60.0, 0, 0, 0, 0, 0), CONFIG)
    assert result.weights == ()
    assert result.failure == "external_fit_nonconverged"
    assert result.iterations == 200


def test_center_outside_the_sample_support_is_rejected() -> None:
    result = fit_joint_weights(VECTORS, BASE, (100, 60, 0, 0, 0, 0, 0), CONFIG)
    assert result.failure == "external_center_outside_support"
    assert not result.weights


def test_downward_adjustment_and_budget_are_explicit() -> None:
    result = fit_joint_weights(VECTORS, BASE, (10, 20, 0, 0, 0, 0, 0), CONFIG)
    mean = fsum(row[0] * w for row, w in zip(VECTORS, result.weights, strict=True))
    assert mean == pytest.approx(11)
    exhausted = fit_joint_weights(
        VECTORS, BASE, (10, 20, 1, 0, 0, 0, 0), replace(CONFIG, fit_iterations=1)
    )
    assert exhausted.failure is not None


def test_support_checks_concentration_and_normalization() -> None:
    assert weight_support((1 / 19,) * 19, CONFIG) == "insufficient_effective_sample"
    assert weight_support((0.11,) + (0.89 / 99,) * 99, CONFIG) == "concentrated_game_weight"
    assert weight_support((0.1,) * 30, CONFIG) == "unnormalized_distribution_weights"
    assert weight_support(BASE, CONFIG) is None


@pytest.mark.parametrize("center", [(float("nan"), 0, 0, 0, 0, 0, 0), (-1, 0, 0, 0, 0, 0, 0), (1,)])
def test_invalid_centers_are_boundary_errors(center: tuple[float, ...]) -> None:
    with pytest.raises(HybridProjectionError):
        fit_joint_weights(VECTORS, BASE, center, CONFIG)
