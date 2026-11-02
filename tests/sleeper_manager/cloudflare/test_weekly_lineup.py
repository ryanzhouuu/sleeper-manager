"""Weekly matchup starters must agree across the dashboard and owner editor."""

import asyncio

import pytest

from sleeper_manager.cloudflare.manager_context import load_manager_context
from tests.sleeper_manager.cloudflare.test_home import (
    FakeSchedule,
    FakeSleeper,
    _build,
    _repository,
)
from tests.sleeper_manager.cloudflare.test_manager_context import ENV
from tests.sleeper_manager.cloudflare.test_manager_context import clients as clients
from tests.sleeper_manager.persistence.test_manager_store import NOW
from tests.sleeper_manager.persistence.test_manager_store import store as store


def test_home_uses_weekly_starter_instead_of_empty_general_slot(tmp_path):  # type: ignore[no-untyped-def]
    sleeper = FakeSleeper()
    sleeper.manager_starters = ["0"]
    sleeper.catalog["known"]["full_name"] = "Caleb Wilson"
    sleeper.matchup_payload[0]["starters"] = ["known"]

    response = _build(sleeper, FakeSchedule(), _repository(tmp_path))

    assert response.status_code == 200
    assert response.payload["roster"][0]["name"] == "Caleb Wilson"
    assert [row["name"] for row in response.payload["roster"]].count("Caleb Wilson") == 1
    assert sleeper.matchup_calls == 1


def test_manager_observes_weekly_starter_instead_of_general_slot(store, clients):  # type: ignore[no-untyped-def]
    sleeper, _ = clients
    sleeper.manager_starters = ["0"]
    sleeper.matchup_payload[0]["starters"] = ["known"]

    context = asyncio.run(load_manager_context(ENV, None, store, NOW))

    assert context.actual_slot("known") == (0, "PG")


@pytest.mark.parametrize("starters", [None, [], ["known", "missing"], [False]])
def test_home_rejects_unverified_weekly_starters(tmp_path, starters):  # type: ignore[no-untyped-def]
    sleeper = FakeSleeper()
    sleeper.matchup_payload[0]["starters"] = starters

    response = _build(sleeper, FakeSchedule(), _repository(tmp_path))

    assert response.status_code == 503


def test_manager_rejects_missing_weekly_lineup(store, clients):  # type: ignore[no-untyped-def]
    sleeper, _ = clients
    sleeper.matchup_payload = []

    with pytest.raises(ValueError, match="weekly lineup"):
        asyncio.run(load_manager_context(ENV, None, store, NOW))
