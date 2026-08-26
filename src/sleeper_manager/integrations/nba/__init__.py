from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sleeper_manager.integrations.nba.espn import ESPNClient
    from sleeper_manager.integrations.nba.sportsdataverse import SportsDataverseClient

__all__ = ["ESPNClient", "SportsDataverseClient"]


def __getattr__(name: str) -> Any:
    if name == "ESPNClient":
        from sleeper_manager.integrations.nba.espn import ESPNClient

        return ESPNClient
    if name == "SportsDataverseClient":
        from sleeper_manager.integrations.nba.sportsdataverse import SportsDataverseClient

        return SportsDataverseClient
    raise AttributeError(name)
