"""Apply factual game absences and expose uncertain status assumptions to live advice."""

import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from hashlib import sha256

from sleeper_manager.domain.nba import AvailabilityStatus
from sleeper_manager.domain.projection import (
    ProjectionDistribution,
    ProjectionReason,
    ProjectionSnapshot,
)
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_models import AvailabilityObservation
from sleeper_manager.projections.hybrid_types import GameAvailability
from sleeper_manager.projections.live_baseline import LiveProjectionTarget


def constrain_live_availability(
    snapshot: ProjectionSnapshot | None,
    target: LiveProjectionTarget,
    report: GameAvailability | None,
    *,
    cutoff: datetime,
    scoring_policy: ScoringPolicy,
    max_age: timedelta,
) -> ProjectionSnapshot | None:
    """Only fresh game confirmations can replace production with deterministic zero mass."""
    if target.game_start <= cutoff or report is None:
        return snapshot
    if (
        report.game_id != target.game_id
        or report.observed_at > cutoff
        or cutoff - report.observed_at > max_age
    ):
        return snapshot
    if report.confirmed_absence:
        fingerprint = sha256(
            json.dumps(
                {"prior": snapshot.input_version if snapshot else None, "report": asdict(report)},
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        return ProjectionSnapshot(
            target.sleeper_player_id,
            target.game_id,
            cutoff,
            f"{snapshot.model_version if snapshot else 'live'}+confirmed-absence-v1",
            f"confirmed-absence-v1-{fingerprint}",
            scoring_policy.version,
            ProjectionDistribution.from_weighted_observations(((0.0, 1.0),)),
            (
                ProjectionReason(
                    "confirmed_game_absence", f"Out for this game; {report.source_version}"
                ),
            ),
        )
    uncertain = (
        report.status
        in (
            AvailabilityStatus.QUESTIONABLE,
            AvailabilityStatus.DOUBTFUL,
            AvailabilityStatus.PROBABLE,
        )
        or report.observation is AvailabilityObservation.MISSING_REPORT
    )
    if snapshot is not None and uncertain:
        message = (
            f"{report.status.value}; participation remains provisional for this game. "
            f"{report.source_version}"
        )
        return replace(
            snapshot,
            reasons=snapshot.reasons
            + (ProjectionReason("availability_uncertainty", message, applied=False),),
        )
    return snapshot
