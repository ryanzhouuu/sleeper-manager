from pathlib import Path

import pytest

from sleeper_manager.config import ManagerPolicy, load_manager_policy


def test_default_policy_is_balanced() -> None:
    policy = load_manager_policy(Path("does-not-exist.toml"))

    assert policy.decision.preset == "balanced"
    assert policy.decision.minimum_confidence == 0.70
    assert policy.version


def test_policy_preset_is_overridden_by_toml(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "policy.toml"
    path.write_text(
        """
[decision]
preset = "conservative"
minimum_confidence = 0.85

[notifications]
quiet_hours_start = "22:00"
quiet_hours_end = "06:30"
urgent_actions_override_quiet_hours = false

[players]
mapping_overrides = { "player-1" = "provider-1" }
""",
        encoding="utf-8",
    )

    policy = load_manager_policy(path)

    assert policy.decision.preset == "conservative"
    assert policy.decision.minimum_confidence == 0.85
    assert policy.notifications.quiet_hours_start == "22:00"
    assert policy.notifications.quiet_hours_end == "06:30"
    assert policy.notifications.urgent_actions_override_quiet_hours is False
    assert policy.players.mapping_overrides == {"player-1": "provider-1"}


def test_policy_defaults_are_constructible() -> None:
    assert ManagerPolicy().notifications.urgent_actions_override_quiet_hours


@pytest.mark.parametrize(
    "toml_text",
    (
        """
[decision]
use_matchup_context = true
""",
        """
[decision]
protect_elite_upside = false
""",
        """
[notifications]
daily_summary = true
""",
        """
[notifications]
injury_alerts = false
""",
        """
[players]
protected_sleeper_ids = ["player-1"]
""",
    ),
)
def test_removed_policy_keys_fail_closed(tmp_path, toml_text: str) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "policy.toml"
    path.write_text(toml_text, encoding="utf-8")

    with pytest.raises(ValueError, match="Removed from version one"):
        load_manager_policy(path)


def test_to_manager_intent_translates_surviving_fields() -> None:
    policy = ManagerPolicy(
        decision={"preset": "aggressive", "minimum_confidence": 0.62},
        notifications={
            "quiet_hours_start": "21:00",
            "quiet_hours_end": "05:00",
            "urgent_actions_override_quiet_hours": False,
        },
        players={"mapping_overrides": {"player-9": "provider-9"}},
    )

    intent = policy.to_manager_intent()

    assert intent.preset == "aggressive"
    assert intent.minimum_confidence == 0.62
    assert intent.quiet_hours_start == "21:00"
    assert intent.quiet_hours_end == "05:00"
    assert intent.urgent_actions_override_quiet_hours is False
    assert intent.version == policy.version
