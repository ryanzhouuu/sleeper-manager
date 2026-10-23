"""JSON controls retain the authentication and write guards used by HTML forms."""

import asyncio
import json
from dataclasses import replace

from sleeper_manager.cloudflare.form_pages import json_page
from sleeper_manager.cloudflare.manager_context import ManagerContext
from sleeper_manager.cloudflare.manager_document import editor_document
from sleeper_manager.cloudflare.manager_routes import manage
from sleeper_manager.cloudflare.worker import Default
from tests.sleeper_manager.cloudflare.test_manager_controls import (
    batch_request,
    owner_request,
)
from tests.sleeper_manager.cloudflare.test_manager_controls import environment as environment
from tests.sleeper_manager.cloudflare.test_manager_games import game, plan
from tests.sleeper_manager.persistence.test_manager_store import NOW, row
from tests.sleeper_manager.persistence.test_manager_store import store as store


def test_editor_document_separates_advice_saved_choices_and_pending_work(store):  # type: ignore[no-untyped-def]
    context = ManagerContext("league", 1, 1, ("PF",), ("player",), {"player": "Player"}, "UTC")
    current = game(store)
    for pending in (False, True):
        result = editor_document(
            context, (current,), [], "proof", now=NOW, pending=pending, plan=plan()
        )
        card = result["active"][0]
        assert result["csrf"] == "proof"
        assert result["pending"] is pending
        assert card["action"] == "not_locked"
        assert card["advisor"] == ("Unavailable" if pending else "Pass")
        assert card["saved"] == "Wait"
        assert card["score"] == 32.8 and card["version"] == 0
        assert result["plan"] == (
            None if pending else {"title": "Team plan", "message": "Current advice"}
        )


def test_saved_and_expired_games_remain_separate_in_the_document(store):  # type: ignore[no-untyped-def]
    context = ManagerContext("league", 1, 1, ("PF",), ("player",), {}, "UTC")
    expired = replace(game(store), key=replace(row(store).key, game_id="past"), action_deadline=NOW)
    result = editor_document(
        context, (row(store), expired), [], "proof", now=NOW, pending=False, plan=None
    )
    assert result["active"] == []
    assert result["saved"][0]["action"] == "locked"
    assert result["past"][0]["state"] == "Passed · lock window closed"


def test_private_document_does_not_bypass_login(environment):  # type: ignore[no-untyped-def]
    worker = Default()
    worker.env = environment
    req = owner_request()
    req.headers.pop("cookie")
    req.headers["accept"] = "application/json"
    result = asyncio.run(worker.fetch(req))
    assert json.loads(result.body) == {"status": "login", "rejected": False}
    assert result.headers["content-type"].startswith("application/json")
    assert result.headers["cache-control"] == "no-store"
    assert "Player" not in result.body and "csrf" not in result.body


def test_json_conflict_still_rejects_the_entire_save(environment, store):  # type: ignore[no-untyped-def]
    response = asyncio.run(manage(batch_request(version_0="9"), environment, None, now=NOW))
    assert json.loads(json_page(response).body)["title"] == "Decision changed"
    assert response.status == 409
    assert row(store).acknowledged_action == "locked"
    assert asyncio.run(store.list_scheduled_work()) == ()


def test_document_history_records_corrections_without_raw_payloads(environment, store):  # type: ignore[no-untyped-def]
    asyncio.run(manage(batch_request(), environment, None, now=NOW))
    response = asyncio.run(manage(owner_request(), environment, None, now=NOW))
    document = json.loads(json_page(response).body)
    assert document["status"] == "ready" and document["pending"]
    assert document["history"] == [
        {
            "name": "Player name",
            "game_id": "game",
            "previous": "locked",
            "action": "not_locked",
            "recorded_at": NOW.isoformat(),
        }
    ]
    assert "previous_state_json" not in json_page(response).body
