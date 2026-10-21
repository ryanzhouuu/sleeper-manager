from sleeper_manager.cloudflare.home_cache import decode_identities
from sleeper_manager.cloudflare.home_document import HomeIdentity


def test_decode_identities_keeps_positions_and_injury_status() -> None:
    decoded = decode_identities(
        {
            "1": {
                "name": "Known Player",
                "nba_team": "NOP",
                "espn_id": "55",
                "positions": ["PG", "SG"],
                "injury_status": "Out",
            }
        }
    )

    assert decoded == {"1": HomeIdentity("1", "Known Player", "NOP", "55", ("PG", "SG"), "Out")}


def test_decode_identities_treats_entries_without_positions_as_a_miss() -> None:
    assert decode_identities({"1": {"name": "Known Player", "nba_team": "NOP"}}) is None
