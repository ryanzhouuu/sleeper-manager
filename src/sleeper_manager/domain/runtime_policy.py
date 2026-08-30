from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time, timedelta
from math import isfinite
from types import MappingProxyType
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PolicyPreset = Literal["conservative", "balanced", "aggressive"]


class RuntimePolicyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ManagerIntent:
    """Versioned manager-authored decision and notification preferences."""

    preset: PolicyPreset
    minimum_confidence: float
    quiet_hours_start: str
    quiet_hours_end: str
    urgent_actions_override_quiet_hours: bool
    protected_sleeper_ids: tuple[str, ...]
    version: str

    def __post_init__(self) -> None:
        if self.preset not in ("conservative", "balanced", "aggressive"):
            raise RuntimePolicyError(
                "Manager intent preset must be conservative, balanced, or aggressive"
            )
        if not isfinite(self.minimum_confidence) or not 0 <= self.minimum_confidence <= 1:
            raise RuntimePolicyError(
                "Manager intent minimum confidence must be between zero and one"
            )
        object.__setattr__(
            self,
            "quiet_hours_start",
            _validate_wall_clock(self.quiet_hours_start, "quiet_hours_start"),
        )
        object.__setattr__(
            self,
            "quiet_hours_end",
            _validate_wall_clock(self.quiet_hours_end, "quiet_hours_end"),
        )
        protected = tuple(item.strip() for item in self.protected_sleeper_ids)
        if any(not item for item in protected):
            raise RuntimePolicyError("Protected Sleeper IDs must be non-empty")
        if len(set(protected)) != len(protected):
            raise RuntimePolicyError("Protected Sleeper IDs must be unique")
        object.__setattr__(self, "protected_sleeper_ids", protected)
        if not self.version.strip():
            raise RuntimePolicyError("Manager intent version must be non-empty")

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> ManagerIntent:
        allowed = {
            "preset",
            "minimum_confidence",
            "quiet_hours_start",
            "quiet_hours_end",
            "urgent_actions_override_quiet_hours",
            "protected_sleeper_ids",
            "version",
        }
        extras = set(payload) - allowed
        if extras:
            raise RuntimePolicyError("Unknown manager intent fields: " + ", ".join(sorted(extras)))
        protected_raw = payload.get("protected_sleeper_ids", ())
        if not isinstance(protected_raw, list):
            raise RuntimePolicyError("protected_sleeper_ids must be a JSON array")
        preset = payload.get("preset", "balanced")
        if not isinstance(preset, str):
            raise RuntimePolicyError("preset must be a string")
        quiet_override = payload.get("urgent_actions_override_quiet_hours", True)
        if not isinstance(quiet_override, bool):
            raise RuntimePolicyError("urgent_actions_override_quiet_hours must be a boolean")
        confidence = payload.get("minimum_confidence", 0.70)
        if isinstance(confidence, bool) or not isinstance(confidence, int | float):
            raise RuntimePolicyError("minimum_confidence must be a number")
        return cls(
            preset=preset,  # type: ignore[arg-type]
            minimum_confidence=float(confidence),
            quiet_hours_start=_required_string(payload, "quiet_hours_start", default="23:00"),
            quiet_hours_end=_required_string(payload, "quiet_hours_end", default="07:00"),
            urgent_actions_override_quiet_hours=quiet_override,
            protected_sleeper_ids=tuple(str(item) for item in protected_raw),
            version=_required_string(payload, "version"),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "preset": self.preset,
            "minimum_confidence": self.minimum_confidence,
            "quiet_hours_start": self.quiet_hours_start,
            "quiet_hours_end": self.quiet_hours_end,
            "urgent_actions_override_quiet_hours": self.urgent_actions_override_quiet_hours,
            "protected_sleeper_ids": list(self.protected_sleeper_ids),
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    version: str
    manager_timezone: str
    daily_plan_time: time
    move_lead_time: timedelta
    sleeper_max_age: timedelta
    availability_max_age: timedelta
    team_data_max_age: timedelta
    projection_history_version: str
    mapping_overrides: Mapping[str, str]
    manager_intent: ManagerIntent

    def __post_init__(self) -> None:
        for label, text_value in (
            ("Runtime policy version", self.version),
            ("Manager timezone", self.manager_timezone),
            ("Projection history version", self.projection_history_version),
        ):
            if not text_value.strip():
                raise RuntimePolicyError(f"{label} must be non-empty")
        try:
            ZoneInfo(self.manager_timezone)
        except ZoneInfoNotFoundError as error:
            raise RuntimePolicyError(
                f"Unknown manager timezone: {self.manager_timezone!r}"
            ) from error
        if self.daily_plan_time.tzinfo is not None:
            raise RuntimePolicyError("Daily plan time must be a local wall-clock time")
        for label, duration in (
            ("Move lead time", self.move_lead_time),
            ("Sleeper freshness", self.sleeper_max_age),
            ("Availability freshness", self.availability_max_age),
            ("Team-data freshness", self.team_data_max_age),
        ):
            if duration <= timedelta(0):
                raise RuntimePolicyError(f"{label} must be positive")
        overrides = {
            str(key).strip(): str(value).strip() for key, value in self.mapping_overrides.items()
        }
        if any(not key or not value for key, value in overrides.items()):
            raise RuntimePolicyError("Mapping override IDs must be non-empty")
        object.__setattr__(self, "mapping_overrides", MappingProxyType(overrides))

    @classmethod
    def from_json(cls, version: str, payload_json: str) -> RuntimePolicy:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as error:
            raise RuntimePolicyError("Runtime policy payload must be valid JSON") from error
        if not isinstance(payload, dict):
            raise RuntimePolicyError("Runtime policy payload must be a JSON object")
        allowed = {
            "manager_timezone",
            "daily_plan_time",
            "move_lead_minutes",
            "sleeper_freshness_minutes",
            "availability_freshness_minutes",
            "team_data_freshness_hours",
            "projection_history_version",
            "mapping_overrides",
            "manager_intent",
        }
        extras = set(payload) - allowed
        if extras:
            raise RuntimePolicyError("Unknown runtime policy fields: " + ", ".join(sorted(extras)))
        try:
            daily_plan_time = time.fromisoformat(_required_string(payload, "daily_plan_time"))
            mapping_overrides = payload.get("mapping_overrides", {})
            if not isinstance(mapping_overrides, dict):
                raise RuntimePolicyError("mapping_overrides must be a JSON object")
            intent_payload = payload.get("manager_intent")
            if intent_payload is None:
                manager_intent = _default_manager_intent()
            elif not isinstance(intent_payload, dict):
                raise RuntimePolicyError("manager_intent must be a JSON object")
            else:
                manager_intent = ManagerIntent.from_json(intent_payload)
            return cls(
                version=version,
                manager_timezone=_required_string(payload, "manager_timezone"),
                daily_plan_time=daily_plan_time,
                move_lead_time=timedelta(minutes=_positive_number(payload, "move_lead_minutes")),
                sleeper_max_age=timedelta(
                    minutes=_positive_number(payload, "sleeper_freshness_minutes")
                ),
                availability_max_age=timedelta(
                    minutes=_positive_number(payload, "availability_freshness_minutes")
                ),
                team_data_max_age=timedelta(
                    hours=_positive_number(payload, "team_data_freshness_hours")
                ),
                projection_history_version=_required_string(payload, "projection_history_version"),
                mapping_overrides=mapping_overrides,
                manager_intent=manager_intent,
            )
        except (TypeError, ValueError) as error:
            if isinstance(error, RuntimePolicyError):
                raise
            raise RuntimePolicyError("Runtime policy contains invalid values") from error

    def to_json(self) -> str:
        payload: dict[str, Any] = {
            "manager_timezone": self.manager_timezone,
            "daily_plan_time": self.daily_plan_time.isoformat(timespec="minutes"),
            "move_lead_minutes": self.move_lead_time.total_seconds() / 60,
            "sleeper_freshness_minutes": self.sleeper_max_age.total_seconds() / 60,
            "availability_freshness_minutes": self.availability_max_age.total_seconds() / 60,
            "team_data_freshness_hours": self.team_data_max_age.total_seconds() / 3600,
            "projection_history_version": self.projection_history_version,
            "mapping_overrides": dict(self.mapping_overrides),
            "manager_intent": self.manager_intent.to_json(),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def default_runtime_policy(*, history_version: str) -> RuntimePolicy:
    return RuntimePolicy(
        version="runtime-policy-v1",
        manager_timezone="America/Chicago",
        daily_plan_time=time(hour=7),
        move_lead_time=timedelta(minutes=10),
        sleeper_max_age=timedelta(minutes=15),
        availability_max_age=timedelta(minutes=15),
        team_data_max_age=timedelta(hours=12),
        projection_history_version=history_version,
        mapping_overrides={},
        manager_intent=_default_manager_intent(),
    )


def _default_manager_intent() -> ManagerIntent:
    from sleeper_manager.config import ManagerPolicy

    return ManagerPolicy().to_manager_intent()


def _validate_wall_clock(value: str, label: str) -> str:
    text = value.strip()
    if not text:
        raise RuntimePolicyError(f"{label} must be non-empty")
    try:
        time.fromisoformat(text)
    except ValueError as error:
        raise RuntimePolicyError(f"{label} must be an HH:MM wall-clock time") from error
    return text


def _required_string(
    payload: Mapping[str, object],
    key: str,
    *,
    default: str | None = None,
) -> str:
    value = payload.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise RuntimePolicyError(f"{key} must be a non-empty string")
    return value.strip()


def _positive_number(payload: Mapping[str, object], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise RuntimePolicyError(f"{key} must be a positive number")
    return float(value)


__all__ = (
    "ManagerIntent",
    "PolicyPreset",
    "RuntimePolicy",
    "RuntimePolicyError",
    "default_runtime_policy",
)
