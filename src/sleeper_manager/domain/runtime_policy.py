from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time, timedelta
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class RuntimePolicyError(ValueError):
    pass


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
        }
        extras = set(payload) - allowed
        if extras:
            raise RuntimePolicyError("Unknown runtime policy fields: " + ", ".join(sorted(extras)))
        try:
            daily_plan_time = time.fromisoformat(_required_string(payload, "daily_plan_time"))
            mapping_overrides = payload.get("mapping_overrides", {})
            if not isinstance(mapping_overrides, dict):
                raise RuntimePolicyError("mapping_overrides must be a JSON object")
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
    )


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimePolicyError(f"{key} must be a non-empty string")
    return value.strip()


def _positive_number(payload: Mapping[str, object], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise RuntimePolicyError(f"{key} must be a positive number")
    return float(value)


__all__ = (
    "RuntimePolicy",
    "RuntimePolicyError",
    "default_runtime_policy",
)
