"""Coverage for historical Lock-In diagnostic admission."""

from __future__ import annotations

from dataclasses import replace

from lock_in_diagnostic_support import _team_week

from sleeper_manager.backtesting.experiments.lock_in_diagnostic import (
    admit_historical_team_week,
)
from sleeper_manager.backtesting.replay.inputs.models import (
    ReplayCoverageSummary,
    ReplayInputExclusion,
)
from sleeper_manager.domain.planning import PlanningQuality, PlanningReasonCode


def test_admission_accepts_exact_and_best_known_complete_evidence() -> None:
    exact = _team_week(eligibility=PlanningQuality.EXACT, exact=1, best_known=0)
    best_known = _team_week(
        eligibility=PlanningQuality.BEST_KNOWN_CONSTRAINTS_ORACLE,
        exact=0,
        best_known=2,
    )

    assert admit_historical_team_week(exact).admitted
    assert admit_historical_team_week(best_known).admitted
    assert not best_known.complete


def test_admission_rejects_missing_projection_coverage() -> None:
    team_week = replace(
        _team_week(),
        coverage=ReplayCoverageSummary(2, 2, 2, 0, 2, 2, projected_player_games=1),
    )

    result = admit_historical_team_week(team_week)

    assert not result.admitted
    assert any(
        check.code == "coverage_counts_agree" and not check.passed for check in result.checks
    )


def test_admission_rejects_decision_critical_exclusion() -> None:
    team_week = replace(
        _team_week(),
        exclusions=(
            ReplayInputExclusion(
                PlanningReasonCode.MISSING_PROJECTION,
                "league-1:week=1:roster=1",
                "Projection evidence was unavailable.",
            ),
        ),
    )

    result = admit_historical_team_week(team_week)

    assert not result.admitted
