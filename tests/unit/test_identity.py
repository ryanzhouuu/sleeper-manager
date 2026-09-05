from datetime import UTC, datetime

import pytest

from sleeper_manager.domain.nba import ProviderPlayer, SourceMetadata
from sleeper_manager.integrations.nba.identity import (
    MappingConfidence,
    MappingMethod,
    PlayerIdentityMapper,
    SleeperPlayerIdentity,
)

SOURCE = SourceMetadata("espn", "test", datetime(2026, 8, 4, tzinfo=UTC))


def provider(player_id: str, name: str, team: str) -> ProviderPlayer:
    return ProviderPlayer(player_id, name, team, team, True, SOURCE)


def test_identity_mapping_uses_override_then_stable_id_then_context() -> None:
    result = PlayerIdentityMapper().resolve(
        [
            SleeperPlayerIdentity("sleeper-override", "Any Name", "DEN"),
            SleeperPlayerIdentity("sleeper-stable", "Nikola Jokic", "DEN", "espn-1"),
            SleeperPlayerIdentity("sleeper-team", "Nikola Jokić", "DEN"),
            SleeperPlayerIdentity("sleeper-name", "Unique Player", None),
        ],
        [provider("espn-1", "Nikola Jokic", "DEN"), provider("espn-2", "Unique Player", "LAL")],
        overrides={"sleeper-override": "espn-1"},
    )

    assert [mapping.method for mapping in result.mappings] == [
        MappingMethod.EXPLICIT_OVERRIDE,
        MappingMethod.STABLE_ID,
        MappingMethod.NORMALIZED_NAME_TEAM,
        MappingMethod.NORMALIZED_NAME_ONLY,
    ]
    assert result.mappings[-1].confidence is MappingConfidence.LOW


def test_identity_mapping_does_not_guess_ambiguous_names_or_invalid_overrides() -> None:
    result = PlayerIdentityMapper().resolve(
        [
            SleeperPlayerIdentity("ambiguous", "Same Name", None),
            SleeperPlayerIdentity("invalid", "Any Name", "DEN"),
        ],
        [provider("espn-1", "Same Name", "DEN"), provider("espn-2", "Same Name", "LAL")],
        overrides={"invalid": "missing"},
    )

    assert len(result.unresolved) == 2
    assert all(mapping.method is MappingMethod.UNRESOLVED for mapping in result.unresolved)
    assert result.warnings


@pytest.mark.parametrize("team", [None, "BOS", "DEN", "LAL"])
@pytest.mark.parametrize("reverse", [False, True])
def test_identity_mapping_counts_one_player_across_duplicate_and_traded_rows(
    team: str | None, reverse: bool
) -> None:
    candidates = [
        provider("espn-1", "Traded Player", "DEN"),
        provider("espn-1", "Traded Player", "DEN"),
        provider("espn-1", "Traded Player", "LAL"),
    ]
    result = PlayerIdentityMapper().resolve(
        [SleeperPlayerIdentity("traded", "Traded Player", team)],
        reversed(candidates) if reverse else candidates,
    )

    mapping = result.mappings[0]
    assert mapping.espn_id == "espn-1"
    if team in {"DEN", "LAL"}:
        assert mapping.method is MappingMethod.NORMALIZED_NAME_TEAM
        assert mapping.confidence is MappingConfidence.MEDIUM
    else:
        assert mapping.method is MappingMethod.NORMALIZED_NAME_ONLY
        assert mapping.confidence is MappingConfidence.LOW


@pytest.mark.parametrize("team", [None, "DEN"])
def test_duplicate_rows_do_not_hide_distinct_players_with_the_same_name(
    team: str | None,
) -> None:
    result = PlayerIdentityMapper().resolve(
        [SleeperPlayerIdentity("ambiguous", "Same Name", team)],
        [
            provider("espn-1", "Same Name", "DEN"),
            provider("espn-1", "Same Name", "DEN"),
            provider("espn-2", "Same Name", "DEN"),
        ],
    )

    mapping = result.mappings[0]
    assert mapping.espn_id is None
    assert mapping.method is MappingMethod.UNRESOLVED
    assert mapping.candidate_ids == ("espn-1", "espn-2")


def test_identity_mapping_preserves_name_aliases_for_one_provider_id() -> None:
    result = PlayerIdentityMapper().resolve(
        [SleeperPlayerIdentity("alias", "Old Name", "DEN")],
        [
            provider("espn-1", "Old Name", "DEN"),
            provider("espn-1", "New Name", "LAL"),
        ],
    )

    assert result.mappings[0].espn_id == "espn-1"
    assert result.mappings[0].method is MappingMethod.NORMALIZED_NAME_TEAM
