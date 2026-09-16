import json
from datetime import time, timedelta

import pytest

from sleeper_manager.config import ManagerPolicy
from sleeper_manager.domain.runtime_policy import (
    ManagerIntent,
    RuntimePolicy,
    RuntimePolicyError,
    default_runtime_policy,
)


def test_default_runtime_policy_uses_approved_central_schedule() -> None:
    policy = default_runtime_policy(history_version="history-2026")

    assert policy.manager_timezone == "America/Chicago"
    assert policy.daily_plan_time == time(7)
    assert policy.move_lead_time == timedelta(minutes=10)
    assert policy.sleeper_max_age == timedelta(minutes=15)
    assert policy.availability_max_age == timedelta(minutes=15)
    assert policy.team_data_max_age == timedelta(hours=12)
    assert policy.manager_intent == ManagerPolicy().to_manager_intent()
    assert RuntimePolicy.from_json(policy.version, policy.to_json()) == policy


def test_runtime_policy_without_manager_intent_uses_defaults() -> None:
    legacy_payload = """{
        "manager_timezone":"America/Chicago",
        "daily_plan_time":"07:00",
        "move_lead_minutes":10,
        "sleeper_freshness_minutes":15,
        "availability_freshness_minutes":15,
        "team_data_freshness_hours":12,
        "projection_history_version":"history-2026",
        "mapping_overrides":{}
    }"""

    loaded = RuntimePolicy.from_json("runtime-policy-v1", legacy_payload)

    assert loaded.manager_intent == ManagerPolicy().to_manager_intent()


def test_manager_intent_round_trips_through_runtime_policy_json() -> None:
    intent = ManagerIntent(
        preset="conservative",
        minimum_confidence=0.85,
        quiet_hours_start="22:30",
        quiet_hours_end="06:15",
        urgent_actions_override_quiet_hours=False,
        version="abc123def4567890",
    )
    policy = default_runtime_policy(history_version="history-2026").__class__(
        version="runtime-policy-v1",
        manager_timezone="America/Chicago",
        daily_plan_time=time(7),
        move_lead_time=timedelta(minutes=10),
        sleeper_max_age=timedelta(minutes=15),
        availability_max_age=timedelta(minutes=15),
        team_data_max_age=timedelta(hours=12),
        projection_history_version="history-2026",
        mapping_overrides={},
        manager_intent=intent,
    )

    restored = RuntimePolicy.from_json(policy.version, policy.to_json())

    assert restored.manager_intent == intent


@pytest.mark.parametrize(
    "payload",
    (
        "not-json",
        "[]",
        '{"manager_timezone":"Mars/Olympus"}',
        """{
            "manager_timezone":"America/Chicago",
            "daily_plan_time":"07:00",
            "move_lead_minutes":0,
            "sleeper_freshness_minutes":15,
            "availability_freshness_minutes":15,
            "team_data_freshness_hours":12,
            "projection_history_version":"history-2026"
        }""",
        """{
            "manager_timezone":"America/Chicago",
            "daily_plan_time":"07:00",
            "move_lead_minutes":10,
            "sleeper_freshness_minutes":15,
            "availability_freshness_minutes":15,
            "team_data_freshness_hours":12,
            "projection_history_version":"history-2026",
            "manager_intent":{"preset":"balanced","minimum_confidence":2}
        }""",
        """{
            "manager_timezone":"America/Chicago",
            "daily_plan_time":"07:00",
            "move_lead_minutes":10,
            "sleeper_freshness_minutes":15,
            "availability_freshness_minutes":15,
            "team_data_freshness_hours":12,
            "projection_history_version":"history-2026",
            "manager_intent":{
                "preset":"balanced",
                "minimum_confidence":0.7,
                "quiet_hours_start":"bad",
                "quiet_hours_end":"07:00",
                "urgent_actions_override_quiet_hours":true,
                "protected_sleeper_ids":[],
                "version":"intent-v1"
            }
        }""",
    ),
)
def test_runtime_policy_rejects_invalid_payload(payload: str) -> None:
    with pytest.raises(RuntimePolicyError):
        RuntimePolicy.from_json("policy-v1", payload)


def test_manager_intent_accepts_empty_legacy_protected_ids() -> None:
    """Permit one safe runtime resynchronization from the removed empty field."""

    payload = default_runtime_policy(history_version="history-2026").to_json()
    decoded = json.loads(payload)
    decoded["manager_intent"]["protected_sleeper_ids"] = []

    restored = RuntimePolicy.from_json("runtime-policy-v1", json.dumps(decoded))

    assert restored.manager_intent == ManagerPolicy().to_manager_intent()


def test_manager_intent_rejects_nonempty_legacy_protected_ids() -> None:
    """Fail closed when a removed preference would otherwise be ignored."""

    payload = default_runtime_policy(history_version="history-2026").to_json()
    decoded = json.loads(payload)
    decoded["manager_intent"]["protected_sleeper_ids"] = ["player-1"]

    with pytest.raises(RuntimePolicyError, match="removed from version one"):
        RuntimePolicy.from_json("runtime-policy-v1", json.dumps(decoded))
