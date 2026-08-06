from datetime import UTC, datetime

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
