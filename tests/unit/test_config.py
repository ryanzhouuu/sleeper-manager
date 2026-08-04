from pathlib import Path

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

[players]
protected_sleeper_ids = ["player-1"]
""",
        encoding="utf-8",
    )

    policy = load_manager_policy(path)

    assert policy.decision.preset == "conservative"
    assert policy.decision.minimum_confidence == 0.85
    assert policy.decision.use_matchup_context is False
    assert policy.players.protected_sleeper_ids == ("player-1",)


def test_policy_defaults_are_constructible() -> None:
    assert ManagerPolicy().notifications.urgent_actions_override_quiet_hours
