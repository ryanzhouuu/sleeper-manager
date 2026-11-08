"""Saved player-game history must not prevent a new decision from being submitted."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.cloudflare.form_pages import form_values
from sleeper_manager.cloudflare.manager_auth import csrf_proof, issue_session
from sleeper_manager.cloudflare.manager_routes import manage
from sleeper_manager.domain.lock_in import LockInOpportunityStatus as Status
from tests.sleeper_manager.cloudflare.form_request_support import request
from tests.sleeper_manager.cloudflare.test_manager_controls import (
    CHANGE_ID,
    SECRET,
    owner_request,
)
from tests.sleeper_manager.cloudflare.test_manager_controls import environment as environment
from tests.sleeper_manager.persistence.test_manager_store import NOW, row
from tests.sleeper_manager.persistence.test_manager_store import store as store


def history_form(store, count):  # type: ignore[no-untyped-def]
    """Include expired, unchanged passes alongside one current decision to correct."""
    session = issue_session(SECRET, NOW)
    values = {
        "csrf": csrf_proof(SECRET, session),
        "batch_id": CHANGE_ID,
        "count": str(count),
    }
    current = row(store)
    for index in range(count):
        record = current
        action = "not_locked"
        if index:
            record = replace(
                current,
                key=replace(current.key, player_id=f"player-{index}", game_id=f"game-{index}"),
                status=Status.ACKNOWLEDGED_PASSED,
                acknowledged_action="passed",
                action_deadline=NOW - timedelta(hours=1),
                current_recommendation_id=None,
            )
            asyncio.run(store.upsert_lock_in_opportunity(record))
            action = "passed"
        values.update(
            {
                f"player_id_{index}": record.key.player_id,
                f"game_id_{index}": record.key.game_id,
                f"version_{index}": str(record.row_version),
                f"action_{index}": action,
            }
        )
    return session, values


@pytest.mark.parametrize("count", [17, 128])
def test_saved_history_does_not_block_one_current_edit(environment, store, count):  # type: ignore[no-untyped-def]
    session, values = history_form(store, count)
    response = asyncio.run(
        manage(
            owner_request("/manage/save", method="POST", values=values, session=session),
            environment,
            None,
            now=NOW,
        )
    )
    assert response.status == 303
    assert row(store).acknowledged_action is None
    assert len(asyncio.run(store.decision_history("league", 1, 1))) == 1
    assert len(asyncio.run(store.list_scheduled_work())) == 1
    saved = asyncio.run(store.list_week_lock_in_opportunities("league", 1))
    assert sum(record.acknowledged_action == "passed" for record in saved) == count - 1


def test_stale_saved_history_rejects_the_whole_large_form(environment, store):  # type: ignore[no-untyped-def]
    session, values = history_form(store, 17)
    values["version_16"] = "99"
    response = asyncio.run(
        manage(
            owner_request("/manage/save", method="POST", values=values, session=session),
            environment,
            None,
            now=NOW,
        )
    )
    assert response.status == 409
    assert row(store).acknowledged_action == "locked"
    assert asyncio.run(store.decision_history("league", 1, 1)) == []
    assert asyncio.run(store.list_scheduled_work()) == ()


def test_form_field_count_remains_bounded():  # type: ignore[no-untyped-def]
    values = {f"field-{index}": "value" for index in range(516)}
    with pytest.raises(ValueError, match="Max number of fields"):
        asyncio.run(form_values(request("POST", values=values)))


@pytest.mark.parametrize("declared", [False, True])
def test_form_byte_count_remains_bounded(declared):  # type: ignore[no-untyped-def]
    req = request("POST", values={"field": "x" * 16384})
    if declared:
        req.headers["content-length"] = "16390"
    with pytest.raises(ValueError, match="Form too large"):
        asyncio.run(form_values(req))
