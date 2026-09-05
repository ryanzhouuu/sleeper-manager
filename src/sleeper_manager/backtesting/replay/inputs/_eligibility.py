"""Select position evidence for replay assembly without owning joins or coverage."""

from collections.abc import Sequence
from datetime import datetime

from sleeper_manager.backtesting.replay.league_archive import PlayerEligibilitySnapshot
from sleeper_manager.domain.planning import PlanningQuality


def _eligibility_at(
    sleeper_id: str,
    cutoff: datetime,
    evidence: Sequence[PlayerEligibilitySnapshot],
) -> tuple[tuple[str, ...], PlanningQuality]:
    snapshots = tuple(
        sorted(
            (snapshot for snapshot in evidence if snapshot.sleeper_id == sleeper_id),
            key=lambda snapshot: snapshot.available_as_of,
        )
    )
    exact = tuple(snapshot for snapshot in snapshots if snapshot.available_as_of <= cutoff)
    if exact:
        snapshot = exact[-1]
        same_time = tuple(
            item for item in exact if item.available_as_of == snapshot.available_as_of
        )
        positions = _consistent_positions(same_time)
        return positions, PlanningQuality.EXACT if positions else PlanningQuality.PARTIAL
    if snapshots:
        positions = _consistent_positions((snapshots[0],))
        return (
            positions,
            PlanningQuality.BEST_KNOWN_CONSTRAINTS_ORACLE if positions else PlanningQuality.PARTIAL,
        )
    return (), PlanningQuality.PARTIAL


def _consistent_positions(
    snapshots: Sequence[PlayerEligibilitySnapshot],
) -> tuple[str, ...]:
    position_sets = {
        tuple(
            sorted(
                {
                    position.strip().upper()
                    for position in snapshot.eligible_positions
                    if position.strip()
                }
            )
        )
        for snapshot in snapshots
    }
    if len(position_sets) != 1:
        return ()
    return next(iter(position_sets), ())
