"""The owner editor shares the home page's cached player identities and headshot matching."""

import asyncio
from types import SimpleNamespace

import pytest

from sleeper_manager.cloudflare.manager_context import load_manager_context
from sleeper_manager.domain.nba import ProviderPlayer, SourceMetadata
from tests.sleeper_manager.cloudflare.test_home import FakeSchedule, FakeSleeper
from tests.sleeper_manager.persistence.test_manager_store import NOW
from tests.sleeper_manager.persistence.test_manager_store import store as store

ENV = SimpleNamespace(SLEEPER_LEAGUE_ID="league", SLEEPER_USER_ID="user-1", TIMEZONE="UTC")


@pytest.fixture
def clients(monkeypatch):  # type: ignore[no-untyped-def]
    sleeper, espn = FakeSleeper(), FakeSchedule()
    sleeper.catalog["known"]["espn_id"] = None
    espn.roster_players = (
        ProviderPlayer(
            provider_id="55",
            full_name="Known Player",
            team_id="no",
            team_abbreviation="NO",
            active=True,
            source=SourceMetadata(provider="espn", provider_id="55", retrieved_at=NOW),
        ),
    )
    monkeypatch.setattr(
        "sleeper_manager.cloudflare.manager_context.CloudflareSleeperClient", lambda _: sleeper
    )
    monkeypatch.setattr(
        "sleeper_manager.cloudflare.manager_context.CloudflareESPNProvider",
        lambda *args, **kwargs: espn,
    )
    return sleeper, espn


def test_manager_resolves_and_reuses_the_home_identity_cache(store, clients):  # type: ignore[no-untyped-def]
    sleeper, espn = clients
    for _ in range(2):
        context = asyncio.run(load_manager_context(ENV, None, store, NOW))
        assert context.names["known"] == "Known Player"
        assert context.identities["known"].espn_id == "55"
        assert context.identities["known"].nba_team == "NOP"
        assert context.actual_slot("known") == (0, "PG")
    assert sleeper.player_calls == 1
    assert espn.roster_calls == ["NOP"]


def test_failed_photo_lookup_keeps_controls_available_and_retries(store, clients):  # type: ignore[no-untyped-def]
    sleeper, espn = clients
    espn.roster_error = RuntimeError("temporary provider failure")
    context = asyncio.run(load_manager_context(ENV, None, store, NOW))
    assert context.names["known"] == "Known Player"
    assert context.identities["known"].espn_id is None
    assert context.actual_slot("known") == (0, "PG")

    espn.roster_error = None
    recovered = asyncio.run(load_manager_context(ENV, None, store, NOW))
    assert recovered.identities["known"].espn_id == "55"
    assert sleeper.player_calls == 2
    assert espn.roster_calls == ["NOP", "NOP"]


def test_missing_catalog_does_not_block_owner_scope(store, clients):  # type: ignore[no-untyped-def]
    sleeper, espn = clients
    sleeper.fail_players = True
    context = asyncio.run(load_manager_context(ENV, None, store, NOW))
    assert context.names == {}
    assert context.identities == {}
    assert context.actual_slot("known") == (0, "PG")
    assert espn.roster_calls == []
