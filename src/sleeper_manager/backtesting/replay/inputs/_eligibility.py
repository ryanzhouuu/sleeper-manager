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
    """Use the latest available positions without upgrading their declared provenance."""
    snapshots = tuple(
        sorted(
            (snapshot for snapshot in evidence if snapshot.sleeper_id == sleeper_id),
            key=lambda snapshot: snapshot.available_as_of,
        )
    )
    if not snapshots:
        return (), PlanningQuality.PARTIAL
    available = tuple(snapshot for snapshot in snapshots if snapshot.available_as_of <= cutoff)
    selected = available[-1] if available else snapshots[0]
    same_time = tuple(
        item for item in snapshots if item.available_as_of == selected.available_as_of
    )
    positions = _consistent_positions(same_time)
    if not positions:
        return (), PlanningQuality.PARTIAL
    exact = bool(available) and all(
        item.confidence == "exact" and item.source.strip() for item in same_time
    )
    quality = PlanningQuality.EXACT if exact else PlanningQuality.BEST_KNOWN_CONSTRAINTS_ORACLE
    return positions, quality


def _consistent_positions(
    snapshots: Sequence[PlayerEligibilitySnapshot],
) -> tuple[str, ...]:
    """Reject conflicting snapshots rather than choosing by input order."""
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
