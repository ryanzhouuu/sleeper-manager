from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables or `.env`."""

    sleeper_league_id: str = ""
    sleeper_user_id: str = ""
    timezone: str = "Pacific/Honolulu"

    ntfy_base_url: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_access_token: SecretStr | None = None

    discord_webhook_url: SecretStr | None = None

    state_backend: Literal["sqlite", "dynamodb"] = "sqlite"
    sqlite_path: Path = Path(".local/state.db")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def sleeper_configured(self) -> bool:
        return bool(self.sleeper_league_id and self.sleeper_user_id)

    @property
    def notifications_configured(self) -> bool:
        return bool(self.ntfy_topic or self.discord_webhook_url)
