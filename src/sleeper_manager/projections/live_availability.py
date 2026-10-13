"""Apply factual game absences and expose uncertain status assumptions to live advice."""

import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from hashlib import sha256

from sleeper_manager.domain.nba import AvailabilityStatus, PlayerAvailability
from sleeper_manager.domain.projection import (
    ProjectionDistribution,
    ProjectionReason,
    ProjectionSnapshot,
)
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_models import AvailabilityObservation
from sleeper_manager.projections.hybrid_types import GameAvailability
from sleeper_manager.projections.live_baseline import LiveProjectionTarget


def _input_version(
    snapshot: ProjectionSnapshot | None, evidence: GameAvailability | PlayerAvailability | str
) -> str:
    """Bind status revisions even when a provisional estimate keeps the same numeric value."""
    raw = evidence if isinstance(evidence, str) else asdict(evidence)
    return sha256(
        json.dumps(
            {"prior": snapshot.input_version if snapshot else None, "evidence": raw},
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()


def constrain_live_availability(
    snapshot: ProjectionSnapshot | None,
    target: LiveProjectionTarget,
    report: GameAvailability | None,
    *,
    cutoff: datetime,
    scoring_policy: ScoringPolicy,
    max_age: timedelta,
    generic_report: PlayerAvailability | None = None,
    availability_gap: str | None = None,
) -> ProjectionSnapshot | None:
    """Only fresh game confirmations can replace production with deterministic zero mass."""
    if target.game_start <= cutoff:
        return snapshot
    applicable = (
        report is not None
        and report.game_id == target.game_id
        and (report.observed_at <= cutoff and cutoff - report.observed_at <= max_age)
    )
    if applicable:
        assert report is not None
        version = _input_version(snapshot, report)
        source = ProjectionReason("game_availability_source", report.source_version, applied=False)
        if report.confirmed_absence:
            return ProjectionSnapshot(
                target.sleeper_player_id,
                target.game_id,
                cutoff,
                f"{snapshot.model_version if snapshot else 'live'}+confirmed-absence-v1",
                f"confirmed-absence-v1-{version}",
                scoring_policy.version,
                ProjectionDistribution.from_weighted_observations(((0.0, 1.0),)),
                (ProjectionReason("confirmed_game_absence", "Out for this game."), source),
            )
        if snapshot is None:
            return None
        reasons = snapshot.reasons + (source,)
        if report.status in (
            AvailabilityStatus.QUESTIONABLE,
            AvailabilityStatus.DOUBTFUL,
            AvailabilityStatus.PROBABLE,
        ):
            reasons += (
                ProjectionReason(
                    "availability_uncertainty",
                    f"{report.status.value.title()}; participation is provisional for this game.",
                    applied=False,
                ),
            )
        elif report.observation is AvailabilityObservation.MISSING_REPORT:
            reasons += (
                ProjectionReason(
                    "availability_uncertainty",
                    "The team has not submitted a game report; participation remains provisional.",
                    applied=False,
                ),
            )
        return replace(snapshot, input_version=f"availability-v1-{version}", reasons=reasons)
    if snapshot is None:
        return None
    if generic_report is not None and (
        generic_report.player_id == target.provider_player_id
        and generic_report.source.retrieved_at <= cutoff
        and cutoff - generic_report.source.retrieved_at <= max_age
        and (
            generic_report.source.source_updated_at is None
            or generic_report.source.source_updated_at <= cutoff
        )
        and generic_report.status
        in (
            AvailabilityStatus.OUT,
            AvailabilityStatus.QUESTIONABLE,
            AvailabilityStatus.DOUBTFUL,
            AvailabilityStatus.PROBABLE,
        )
    ):
        reason = ProjectionReason(
            "availability_uncertainty",
            f"An unscoped {generic_report.status.value} designation is available; "
            "its game applicability is unverified and participation remains provisional.",
            applied=False,
        )
        return replace(
            snapshot,
            input_version=f"availability-v1-{_input_version(snapshot, generic_report)}",
            reasons=snapshot.reasons + (reason,),
        )
    if availability_gap is not None:
        return replace(
            snapshot,
            input_version=f"availability-gap-v1-{_input_version(snapshot, availability_gap)}",
            reasons=snapshot.reasons
            + (ProjectionReason("availability_data_gap", availability_gap, applied=False),),
        )
    return snapshot
