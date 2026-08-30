"""Environment settings and the optional local manager-policy TOML file.

`Settings` is the local CLI surface (`STATE_BACKEND` is sqlite-only). The
Cloudflare Worker reads destination URLs and the D1 binding from its env, not
this class. Missing policy files resolve to `ManagerPolicy` defaults.
"""

import json
import tomllib
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from sleeper_manager.domain.runtime_policy import ManagerIntent

PolicyPreset = Literal["conservative", "balanced", "aggressive"]

_REMOVED_DECISION_KEYS = frozenset({"use_matchup_context", "protect_elite_upside"})
_REMOVED_NOTIFICATION_KEYS = frozenset({"daily_summary", "injury_alerts"})
_REMOVED_PLAYER_KEYS = frozenset({"protected_sleeper_ids"})
_REMOVED_FROM_VERSION_ONE = (
    _REMOVED_DECISION_KEYS | _REMOVED_NOTIFICATION_KEYS | _REMOVED_PLAYER_KEYS
)


class DecisionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    preset: PolicyPreset = "balanced"
    minimum_confidence: float = Field(default=0.70, ge=0, le=1)


class NotificationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    quiet_hours_start: str = "23:00"
    quiet_hours_end: str = "07:00"
    urgent_actions_override_quiet_hours: bool = True


class PlayerPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mapping_overrides: dict[str, str] = Field(default_factory=dict)


class ManagerPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: DecisionPolicy = Field(default_factory=DecisionPolicy)
    notifications: NotificationPolicy = Field(default_factory=NotificationPolicy)
    players: PlayerPolicy = Field(default_factory=PlayerPolicy)

    @property
    def version(self) -> str:
        """Stable 16-hex digest of the canonical JSON payload."""
        payload = self.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return sha256(encoded).hexdigest()[:16]

    def to_manager_intent(self) -> ManagerIntent:
        """Translate local manager intent into the runtime policy envelope."""

        return ManagerIntent(
            preset=self.decision.preset,
            minimum_confidence=self.decision.minimum_confidence,
            quiet_hours_start=self.notifications.quiet_hours_start,
            quiet_hours_end=self.notifications.quiet_hours_end,
            urgent_actions_override_quiet_hours=(
                self.notifications.urgent_actions_override_quiet_hours
            ),
            version=self.version,
        )


_PRESET_VALUES: dict[PolicyPreset, dict[str, Any]] = {
    "conservative": {"minimum_confidence": 0.80},
    "balanced": {"minimum_confidence": 0.70},
    "aggressive": {"minimum_confidence": 0.60},
}


def load_manager_policy(path: Path) -> ManagerPolicy:
    """Load policy TOML, applying preset defaults then file overrides.

    Missing files return defaults. Unknown presets raise ValueError. Extra keys
    inside known tables are rejected. Removed version-one keys fail closed.
    """
    if not path.exists():
        return ManagerPolicy()

    with path.open("rb") as file:
        raw = tomllib.load(file)

    decision_values = raw.get("decision", {})
    if not isinstance(decision_values, dict):
        raise ValueError("The [decision] policy section must be a TOML table")
    notification_values = raw.get("notifications", {})
    if not isinstance(notification_values, dict):
        raise ValueError("The [notifications] policy section must be a TOML table")
    _reject_removed_policy_keys(decision_values, section="decision")
    _reject_removed_policy_keys(notification_values, section="notifications")
    player_values = raw.get("players", {})
    if not isinstance(player_values, dict):
        raise ValueError("The [players] policy section must be a TOML table")
    _reject_removed_policy_keys(player_values, section="players")

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
        "notifications": notification_values,
        "players": player_values,
    }
    return ManagerPolicy.model_validate(resolved)


def _reject_removed_policy_keys(values: dict[str, object], *, section: str) -> None:
    removed = sorted(key for key in values if key in _REMOVED_FROM_VERSION_ONE)
    if removed:
        joined = ", ".join(removed)
        raise ValueError(
            f"Removed from version one in [{section}]: {joined}. "
            "Delete these keys from the manager policy file."
        )


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables or `.env`."""

    sleeper_league_id: str = ""
    sleeper_user_id: str = ""
    timezone: str = "America/Chicago"
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
