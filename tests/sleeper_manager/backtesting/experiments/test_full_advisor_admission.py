"""Full replay validates supplied cutoff evidence independently of bundle projections."""

from dataclasses import replace

import pytest

from sleeper_manager.backtesting.experiments.full_advisor_replay import (
    FullAdvisorReplayError,
    FullAdvisorReplayRequest,
    run_full_advisor_replay,
)
from sleeper_manager.backtesting.experiments.lock_in_diagnostic import admit_historical_team_week
from sleeper_manager.backtesting.replay.inputs.models import ReplayInputExclusion
from sleeper_manager.backtesting.replay.projection_surface import team_week_fingerprint
from sleeper_manager.decisions.lock_in import LockInPolicyConfig
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.domain.planning import PlanningReasonCode
from tests.sleeper_manager.backtesting.experiments.lock_in_diagnostic_support import (
    _projection_surface,
    _team_week,
)


def request_without_bundle_projections() -> FullAdvisorReplayRequest:
    """Retain genuine coverage gaps while supplying complete independent forecasts."""
    original = _team_week()
    week = replace(
        original,
        player_games=tuple(replace(row, projection=None) for row in original.player_games),
        coverage=replace(
            original.coverage,
            projected_player_games=0,
            missing_evidence=((PlanningReasonCode.MISSING_PROJECTION, len(original.player_games)),),
        ),
        exclusions=(
            ReplayInputExclusion(
                PlanningReasonCode.MISSING_PROJECTION, "original-projections", "No warmup history"
            ),
        ),
    )
    surface = replace(
        _projection_surface(original), team_week_fingerprint=team_week_fingerprint(week)
    )
    return FullAdvisorReplayRequest(
        week,
        surface,
        weekly_policy_config=WeeklyPlanPolicyConfig(scenario_count=32),
        lock_in_policy_config=LockInPolicyConfig(scenario_count=32),
        minimum_confidence=0,
    )


def test_complete_surface_replays_without_embedded_projections() -> None:
    request = request_without_bundle_projections()
    assert not admit_historical_team_week(request.team_week).admitted
    result = run_full_advisor_replay(request)
    assert result.status == "success"
    assert all(passed for _, passed in result.invariant_results)
    assert request.team_week.coverage.projected_player_games == 0
    assert all(row.projection is None for row in request.team_week.player_games)


@pytest.mark.parametrize("case", ["missing_entry", "wrong_fingerprint", "wrong_scoring", "failure"])
def test_missing_bundle_projections_never_bypass_surface_validation(case: str) -> None:
    request = request_without_bundle_projections()
    surface = request.projection_surface
    if case == "missing_entry":
        surface = replace(surface, entries=surface.entries[:-1])
    elif case == "wrong_fingerprint":
        surface = replace(surface, team_week_fingerprint="different")
    elif case == "wrong_scoring":
        surface = replace(surface, scoring_policy_version="different")
    else:
        last = replace(
            surface.entries[-1],
            projection=None,
            failure_reason="missing_warmup_history",
            failure_detail="No warmup",
        )
        surface = replace(surface, entries=(*surface.entries[:-1], last))
    with pytest.raises(FullAdvisorReplayError, match="projection_surface"):
        run_full_advisor_replay(replace(request, projection_surface=surface))


@pytest.mark.parametrize(
    "field", ["joined_player_games", "resolved_identities", "scored_player_games"]
)
def test_independent_projection_evidence_does_not_waive_other_coverage(field: str) -> None:
    request = request_without_bundle_projections()
    week = replace(request.team_week, coverage=replace(request.team_week.coverage, **{field: 0}))
    surface = replace(request.projection_surface, team_week_fingerprint=team_week_fingerprint(week))
    with pytest.raises(FullAdvisorReplayError, match="coverage_counts_agree"):
        run_full_advisor_replay(replace(request, team_week=week, projection_surface=surface))


@pytest.mark.parametrize(
    "reason",
    [
        PlanningReasonCode.MISSING_MEMBERSHIP,
        PlanningReasonCode.UNSUPPORTED_SCORING,
        PlanningReasonCode.MISSING_GAME_SCHEDULE,
    ],
)
def test_independent_projection_evidence_does_not_waive_other_exclusions(
    reason: PlanningReasonCode,
) -> None:
    request = request_without_bundle_projections()
    week = replace(request.team_week, exclusions=(ReplayInputExclusion(reason, "fixture", "gap"),))
    surface = replace(request.projection_surface, team_week_fingerprint=team_week_fingerprint(week))
    with pytest.raises(FullAdvisorReplayError, match="no_decision_critical_exclusions"):
        run_full_advisor_replay(replace(request, team_week=week, projection_surface=surface))
