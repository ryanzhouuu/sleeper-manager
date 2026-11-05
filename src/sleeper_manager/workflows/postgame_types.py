"""Public postgame outcomes and provider seam, separate from orchestration."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from sleeper_manager.domain.lock_in import LockInEvaluation
from sleeper_manager.domain.nba import GameSummary, ProviderResult
from sleeper_manager.persistence.base import RecommendationRecord

LOCK_IN_DECISION_TYPE = "live_lock_in"
LOCK_IN_WARNING_TYPE = "live_lock_in_unavailable"
LOCK_IN_ACKNOWLEDGEMENT_KINDS = frozenset({LOCK_IN_DECISION_TYPE, "placeholder_lock_in"})


class DirectGameSummarySource(Protocol):
    """Fetch an uncached summary; `players` maps provider player IDs to Sleeper IDs."""

    async def __call__(
        self, game_id: str, players: Mapping[str, str]
    ) -> ProviderResult[GameSummary]: ...


@dataclass(frozen=True, slots=True)
class PostgameLockInResult:
    """Report whether a postgame wake should complete or remain scheduled."""

    outcome: Literal[
        "wait",
        "notified",
        "duplicate",
        "unavailable",
        "automatic_final",
        "delivery_failed",
    ]
    recommendation: RecommendationRecord | None = None
    evaluation: LockInEvaluation | None = None
