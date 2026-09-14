"""Tests for strict paired-policy admission and Phase 7 metric direction."""

from __future__ import annotations

import pytest

from sleeper_manager.backtesting.replay.policy_comparison import (
    ComparablePolicyScore,
    concentration_shares,
    interval_direction,
    paired_policy_regret_delta,
)
from sleeper_manager.backtesting.replay.validation import BootstrapInterval, ReplayValidationError


def score(
    key: str,
    policy_score: float,
    oracle_score: float = 100,
    *,
    fingerprint: str = "shared",
    complete: bool = True,
    invariants: tuple[tuple[str, bool], ...] = (("legal", True),),
) -> ComparablePolicyScore:
    """Build one compact comparison record with admissible defaults."""

    return ComparablePolicyScore(
        team_week_key=key,
        score=policy_score,
        oracle_score=oracle_score,
        compatibility_fingerprint=fingerprint,
        invariant_results=invariants,
        complete=complete,
    )


def test_regret_delta_uses_candidate_minus_baseline_direction() -> None:
    """Report a better candidate as negative regret delta and positive improvement."""

    result = paired_policy_regret_delta(
        [score("a", 90), score("candidate-only", 50), score("blocked", 1, complete=False)],
        [score("a", 80), score("baseline-only", 50)],
        samples=25,
    )

    assert result.common_team_week_count == 1
    assert result.mean_candidate_minus_baseline_regret == -10
    assert result.mean_improvement == 10
    assert result.mean_capture_change == 0.1
    assert result.candidate_unpaired_count == 1
    assert result.baseline_unpaired_count == 1
    assert result.candidate_incomplete_count == 1


def test_capture_change_omits_shared_nonpositive_oracles() -> None:
    """Keep nonpositive-oracle pairs in regret while excluding their capture ratios."""

    result = paired_policy_regret_delta(
        [score("positive", 90), score("zero", 0, 0)],
        [score("positive", 80), score("zero", -2, 0)],
        samples=25,
    )

    assert result.common_team_week_count == 2
    assert result.nonpositive_oracle_pair_count == 1
    assert result.mean_candidate_minus_baseline_regret == -6
    assert result.mean_capture_change == 0.1


@pytest.mark.parametrize(
    ("candidate", "baseline", "message"),
    [
        (score("a", 90, fingerprint="new"), score("a", 80), "Incompatible paired inputs"),
        (score("a", 90, 101), score("a", 80), "Mismatched paired oracle"),
        (
            score("a", 90, invariants=(("legal", False),)),
            score("a", 80),
            "Failed paired invariant",
        ),
    ],
)
def test_pair_admission_rejects_incompatible_evidence(
    candidate: ComparablePolicyScore,
    baseline: ComparablePolicyScore,
    message: str,
) -> None:
    """Reject rows whose compatibility, oracle, or invariant evidence differs."""

    with pytest.raises(ReplayValidationError, match=message):
        paired_policy_regret_delta([candidate], [baseline])


def test_pair_admission_rejects_duplicates_before_completeness_filter() -> None:
    """Require an explicit version choice even when one duplicate is incomplete."""

    with pytest.raises(ReplayValidationError, match="Duplicate candidate team-week"):
        paired_policy_regret_delta([score("a", 90), score("a", 90, complete=False)], [])


def test_interval_direction_keeps_zero_crossings_inconclusive() -> None:
    """Distinguish directional evidence from intervals that include zero."""

    assert interval_direction(BootstrapInterval(0.01, 0.2)) == "improved"
    assert interval_direction(BootstrapInterval(-0.2, -0.01)) == "regressed"
    assert interval_direction(BootstrapInterval(-0.2, 0.01)) == "inconclusive"
    assert interval_direction(None) == "insufficient_evidence"


def test_concentration_requires_positive_net_improvement() -> None:
    """Keep signed shares above one and leave nonpositive denominators undefined."""

    positive = concentration_shares({"team-a": 12, "team-b": -2})

    assert positive.net_improvement == 10
    assert positive.shares == (("team-a", 1.2), ("team-b", -0.2))
    assert concentration_shares({"team-a": 2, "team-b": -2}).shares is None
    assert concentration_shares({"team-a": -1}).shares is None
