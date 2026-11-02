"""Weekly lineup validation rejects ambiguity before any consumer builds advice."""

import pytest

from sleeper_manager.integrations.sleeper.schemas import SleeperRosterPayload
from sleeper_manager.integrations.sleeper.weekly_lineup import with_matchup_starters


def _roster() -> SleeperRosterPayload:
    return SleeperRosterPayload(
        roster_id=1,
        owner_id="owner",
        players=["caleb", "guard", "reserve"],
        starters=["0", "guard"],
        reserve=["reserve"],
    )


def test_selects_owned_roster_and_keeps_ownership_and_reserve() -> None:
    roster = _roster()
    weekly = with_matchup_starters(
        roster,
        [
            {"roster_id": 2, "starters": ["other", "0"]},
            {"roster_id": 1, "starters": ["caleb", "guard"]},
        ],
        ["PF", "PG", "BN", "IR"],
    )
    assert weekly.starters == ["caleb", "guard"]
    assert weekly.players == roster.players
    assert weekly.reserve == roster.reserve
    assert roster.starters == ["0", "guard"]


@pytest.mark.parametrize(
    "starters",
    [
        None,
        [],
        ["caleb"],
        ["caleb", "guard", "0"],
        "caleb",
        ["caleb", False],
        ["caleb", 12],
        ["caleb", " caleb "],
        ["reserve", "guard"],
    ],
)
def test_rejects_ambiguous_weekly_slots(starters):  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="weekly lineup"):
        with_matchup_starters(_roster(), [{"roster_id": 1, "starters": starters}], ["PF", "PG"])


@pytest.mark.parametrize(
    "matchups",
    [
        [],
        [{"roster_id": 2, "starters": ["caleb", "guard"]}],
        [{"roster_id": True, "starters": ["caleb", "guard"]}],
        [{"roster_id": 1, "starters": ["caleb", "guard"]}] * 2,
    ],
)
def test_requires_one_unambiguous_roster(matchups):  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="weekly lineup"):
        with_matchup_starters(_roster(), matchups, ["PF", "PG"])


def test_preserves_empty_slots_without_restoring_departed_players() -> None:
    weekly = with_matchup_starters(
        _roster(), [{"roster_id": 1, "starters": ["departed", " "]}], ["PF", "PG"]
    )
    assert weekly.starters == ["0", "0"]
    assert "departed" not in weekly.players
