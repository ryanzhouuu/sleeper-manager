from datetime import datetime
from typing import Protocol


class StateRepository(Protocol):
    def initialize(self) -> None: ...

    def record_lock_acknowledgement(
        self,
        recommendation_id: str,
        player_id: str,
        acknowledged_at: datetime,
    ) -> None: ...

    def is_locked(self, recommendation_id: str) -> bool: ...
