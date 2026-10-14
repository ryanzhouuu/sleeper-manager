"""Apply an explicitly selected hybrid batch without silently substituting control advice."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from sleeper_manager.domain.projection import ProjectionSnapshot
from sleeper_manager.domain.runtime_policy import RuntimePolicy
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.projections.hybrid_model import HybridProjectionResult
from sleeper_manager.projections.hybrid_types import GameAvailability
from sleeper_manager.projections.live_baseline import LiveProjectionTarget
from sleeper_manager.workflows.planning_collection import CollectedLiveEvidence
from sleeper_manager.workflows.planning_projections import project_live_targets


@dataclass(frozen=True, slots=True)
class _PreparedHybrid:
    """Reuse the computed snapshots through the common availability boundary."""

    snapshots: Mapping[tuple[str, str], ProjectionSnapshot | None]

    def project(
        self,
        target: LiveProjectionTarget,
        *,
        scoring_policy: ScoringPolicy,
        decision_time: datetime,
    ) -> ProjectionSnapshot | None:
        """Missing or failed candidate keys remain explicit projection gaps."""
        return self.snapshots.get((target.sleeper_player_id, target.game_id))


async def apply_active_hybrid(
    manager: CollectedLiveEvidence,
    attempts: Sequence[HybridProjectionResult],
    *,
    policy: RuntimePolicy,
    cutoff: datetime,
    availability: Mapping[tuple[str, str], GameAvailability],
) -> CollectedLiveEvidence:
    """Preserve common status warnings and confirmed absences, including candidate failures."""
    provider = _PreparedHybrid(
        {(row.target.sleeper_player_id, row.target.game_id): row.projection for row in attempts}
    )
    projections = await project_live_targets(
        manager.projection_targets,
        projection_provider=provider,
        scoring_policy=manager.inputs.league_profile.scoring,
        decision_time=cutoff,
        availability=availability,
        max_availability_age=policy.availability_max_age,
        generic_availability={
            row.player_id: row
            for result in manager.inputs.availability_results
            for row in result.records
        },
        availability_gap=next(
            (
                warning
                for warning in manager.warnings
                if warning.startswith(
                    (
                        "official_availability_",
                        "stale_official_",
                        "future_official_",
                        "unverified_official_",
                    )
                )
            ),
            None,
        ),
    )
    return replace(manager, inputs=replace(manager.inputs, projections=projections))
