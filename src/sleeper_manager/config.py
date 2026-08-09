import json
import tomllib
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PolicyPreset = Literal["conservative", "balanced", "aggressive"]


class DecisionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    preset: PolicyPreset = "balanced"
    minimum_confidence: float = Field(default=0.70, ge=0, le=1)
    use_matchup_context: bool = True
    protect_elite_upside: bool = True


class NotificationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    daily_summary: bool = True
    injury_alerts: bool = True
    quiet_hours_start: str = "23:00"
    quiet_hours_end: str = "07:00"
    urgent_actions_override_quiet_hours: bool = True


class PlayerPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protected_sleeper_ids: tuple[str, ...] = ()
    mapping_overrides: dict[str, str] = Field(default_factory=dict)


class ManagerPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: DecisionPolicy = Field(default_factory=DecisionPolicy)
    notifications: NotificationPolicy = Field(default_factory=NotificationPolicy)
    players: PlayerPolicy = Field(default_factory=PlayerPolicy)

    @property
    def version(self) -> str:
        payload = self.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return sha256(encoded).hexdigest()[:16]


_PRESET_VALUES: dict[PolicyPreset, dict[str, Any]] = {
    "conservative": {
        "minimum_confidence": 0.80,
        "use_matchup_context": False,
        "protect_elite_upside": True,
    },
    "balanced": {
        "minimum_confidence": 0.70,
        "use_matchup_context": True,
        "protect_elite_upside": True,
    },
    "aggressive": {
        "minimum_confidence": 0.60,
        "use_matchup_context": True,
        "protect_elite_upside": False,
    },
}


def load_manager_policy(path: Path) -> ManagerPolicy:
    if not path.exists():
        return ManagerPolicy()

    with path.open("rb") as file:
        raw = tomllib.load(file)

    decision_values = raw.get("decision", {})
    if not isinstance(decision_values, dict):
        raise ValueError("The [decision] policy section must be a TOML table")
    preset = decision_values.get("preset", "balanced")
    if preset not in _PRESET_VALUES:
        raise ValueError(f"Unknown manager policy preset: {preset!r}")

    resolved_decision = {
        "preset": preset,
        **_PRESET_VALUES[preset],
        **decision_values,
    }
    resolved = {
        "decision": resolved_decision,
        "notifications": raw.get("notifications", {}),
        "players": raw.get("players", {}),
    }
    return ManagerPolicy.model_validate(resolved)


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables or `.env`."""

    sleeper_league_id: str = ""
    sleeper_user_id: str = ""
    timezone: str = "Pacific/Honolulu"
    manager_policy_path: Path = Path(".local/policy.toml")

    ntfy_base_url: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_access_token: SecretStr | None = None

    discord_webhook_url: SecretStr | None = None

    state_backend: Literal["sqlite"] = "sqlite"
    sqlite_path: Path = Path(".local/state.db")
    acknowledgement_base_url: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def sleeper_configured(self) -> bool:
        return bool(self.sleeper_league_id and self.sleeper_user_id)

    @property
    def notifications_configured(self) -> bool:
        return bool(self.ntfy_topic or self.discord_webhook_url)

    def load_manager_policy(self) -> ManagerPolicy:
        return load_manager_policy(self.manager_policy_path)
