from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sleeper_manager.integrations.sleeper.client import SleeperClient

__all__ = ["SleeperClient"]


def __getattr__(name: str) -> Any:
    if name == "SleeperClient":
        from sleeper_manager.integrations.sleeper.client import SleeperClient

        return SleeperClient
    raise AttributeError(name)
