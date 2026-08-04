from dataclasses import dataclass
from typing import Protocol

from sleeper_manager.domain.models import Recommendation


@dataclass(frozen=True, slots=True)
class LockInContext:
    player_id: str
    completed_score: float
    remaining_game_projections: tuple[float, ...]
    matchup_margin: float


class LockInPolicy(Protocol):
    def evaluate(self, context: LockInContext) -> Recommendation: ...
