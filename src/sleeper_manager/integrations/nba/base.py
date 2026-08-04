from datetime import date
from typing import Any, Protocol


class NBAProvider(Protocol):
    async def scoreboard(self, game_date: date) -> dict[str, Any]: ...

    async def game_summary(self, game_id: str) -> dict[str, Any]: ...

    async def injuries(self) -> dict[str, Any]: ...
