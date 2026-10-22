"""Retained eligibility observations; a pre-tipoff poll is evidence, not an exact tipoff read."""

from dataclasses import dataclass
from datetime import datetime

from sleeper_manager.domain.lock_in import LockInOpportunityStatus


@dataclass(frozen=True, slots=True)
class PlayerGameEvidence:
    """Keep observed eligibility loss distinct from recommended locks and passes."""

    player_id: str
    game_id: str
    roster_id: int
    status: LockInOpportunityStatus
    observed_at: datetime
    rostered_at_tipoff: bool | None
    roster_evidence_at: datetime | None
    action_deadline: datetime
    stable_score: float | None = None
    stabilized_at: datetime | None = None
    score_is_current: bool = False
