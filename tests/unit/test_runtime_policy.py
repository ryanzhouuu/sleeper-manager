from datetime import time, timedelta

import pytest

from sleeper_manager.domain.runtime_policy import (
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
    assert RuntimePolicy.from_json(policy.version, policy.to_json()) == policy


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
    ),
)
def test_runtime_policy_rejects_invalid_payload(payload: str) -> None:
    with pytest.raises(RuntimePolicyError):
        RuntimePolicy.from_json("policy-v1", payload)
