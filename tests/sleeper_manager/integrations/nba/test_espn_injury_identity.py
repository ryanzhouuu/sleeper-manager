from datetime import UTC, datetime

import pytest

from sleeper_manager.domain.nba import AvailabilityStatus
from sleeper_manager.integrations.nba.espn import ESPNSchemaError, parse_injuries

NOW = datetime(2026, 10, 18, tzinfo=UTC)
PROFILE = "https://www.espn.com/nba/player/_/id/12345/example-player"


def _payload(athlete: dict[str, object]) -> dict[str, object]:
    return {
        "injuries": [
            {
                "id": "team-1",
                "injuries": [
                    {
                        "id": "injury-999",
                        "athlete": athlete,
                        "status": "Day-To-Day",
                        "date": "2026-10-18T00:00:00Z",
                    }
                ],
            }
        ]
    }


@pytest.mark.parametrize("explicit_id", [None, "", "12345", 12345])
def test_profile_links_restore_missing_ids_and_agree_with_explicit_ids(explicit_id) -> None:  # type: ignore[no-untyped-def]
    athlete = {
        "id": explicit_id,
        "links": [{"href": PROFILE, "rel": ["playercard", "athlete"]}] * 2,
    }

    result = parse_injuries(_payload(athlete), retrieved_at=NOW)

    assert result.quality.state.value == "fresh"
    assert len(result.records) == 1
    record = result.records[0]
    assert record.player_id == record.source.provider_id == "12345"
    assert record.status is AvailabilityStatus.QUESTIONABLE
    assert record.source.retrieved_at == record.source.source_updated_at == NOW


@pytest.mark.parametrize("explicit_id", ["legacy-id", 12345])
def test_explicit_id_remains_supported_without_links(explicit_id) -> None:  # type: ignore[no-untyped-def]
    result = parse_injuries(_payload({"id": explicit_id}), retrieved_at=NOW)
    assert result.records[0].player_id == str(explicit_id)


@pytest.mark.parametrize(
    "href",
    [
        "https://espn.com/nba/player/_/id/12345",
        "https://www.espn.com/nba/player/_/id/12345/",
        PROFILE + "?platform=desktop",
    ],
)
def test_canonical_profile_url_variations(href: str) -> None:
    result = parse_injuries(_payload({"links": [{"href": href}]}), retrieved_at=NOW)
    assert result.records[0].player_id == "12345"


def test_missing_athlete_does_not_treat_injury_id_as_player_id() -> None:
    with pytest.raises(ESPNSchemaError, match="injury.athlete"):
        parse_injuries({"injuries": [{"id": "injury-999", "status": "Out"}]}, retrieved_at=NOW)


@pytest.mark.parametrize(
    "href",
    [
        "https://espn.com.evil.test/nba/player/_/id/12345/example",
        "https://www.espn.com@evil.test/nba/player/_/id/12345/example",
        "https://evil.test@www.espn.com/nba/player/_/id/12345/example",
        "http://www.espn.com/nba/player/_/id/12345/example",
        "https://www.espn.com/nfl/player/_/id/12345/example",
        "https://www.espn.com/nba/team/_/id/12345/example",
        "https://www.espn.com/nba/player/_/id/not-an-id/example",
        "https://www.espn.com/nba/player/_/id/0/example",
        "https://www.espn.com/nba/player/_/id/12345/example/extra",
        "https://[invalid",
        None,
    ],
)
def test_invalid_links_cannot_supply_an_identity(href) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ESPNSchemaError, match="identity"):
        parse_injuries(_payload({"links": [{"href": href}]}), retrieved_at=NOW)


@pytest.mark.parametrize("links", [None, {}, [None, "invalid"], []])
def test_missing_identity_does_not_fall_back_to_injury_id(links) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ESPNSchemaError, match="identity"):
        parse_injuries(_payload({"links": links}), retrieved_at=NOW)


@pytest.mark.parametrize("explicit_id", [None, "67890"])
def test_conflicting_ids_fail_closed(explicit_id) -> None:  # type: ignore[no-untyped-def]
    links = [{"href": PROFILE}]
    if explicit_id is None:
        links.append({"href": PROFILE.replace("12345", "67890")})
    with pytest.raises(ESPNSchemaError, match="conflicting"):
        parse_injuries(_payload({"id": explicit_id, "links": links}), retrieved_at=NOW)


@pytest.mark.parametrize("explicit_id", [True, False, [], {}, 1.5])
def test_malformed_explicit_ids_are_not_silently_replaced(explicit_id) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ESPNSchemaError, match="identity"):
        parse_injuries(
            _payload({"id": explicit_id, "links": [{"href": PROFILE}]}), retrieved_at=NOW
        )
