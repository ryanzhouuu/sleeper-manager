"""Validate historical facts and the independent projection surface before replay starts."""

from sleeper_manager.backtesting.experiments.full_advisor_replay_models import (
    FullAdvisorReplayError,
    FullAdvisorReplayRequest,
)
from sleeper_manager.backtesting.experiments.lock_in_diagnostic import admit_historical_team_week
from sleeper_manager.backtesting.replay.projection_surface import (
    HistoricalProjectionSurfaceError,
    validate_historical_projection_surface,
)


def validate_full_advisor_inputs(request: FullAdvisorReplayRequest) -> None:
    """Require complete cutoff forecasts without altering legacy bundle coverage evidence."""
    admission = admit_historical_team_week(request.team_week, require_bundle_projections=False)
    if not admission.admitted:
        failed = ",".join(check.code for check in admission.checks if not check.passed)
        raise FullAdvisorReplayError(f"admission_failed:{failed}")
    try:
        validate_historical_projection_surface(
            request.projection_surface,
            team_week=request.team_week,
            planning_lead_time=request.planning_lead_time,
        )
    except HistoricalProjectionSurfaceError as error:
        raise FullAdvisorReplayError(f"projection_surface_invalid:{error}") from error
    for entry in request.projection_surface.entries:
        if entry.projection is None:
            raise FullAdvisorReplayError(
                f"projection_surface_failure:{entry.player_id}:{entry.game_id}:"
                f"{entry.failure_reason}:cutoff={entry.decision_time.isoformat()}"
            )
