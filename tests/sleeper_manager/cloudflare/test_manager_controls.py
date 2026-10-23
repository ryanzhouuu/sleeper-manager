"""Owner identity, CSRF and immutable game scope protect dashboard edits."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from sleeper_manager.cloudflare.manager_auth import (
    COOKIE_NAME,
    SESSION_SECONDS,
    csrf_proof,
    issue_session,
    read_session,
    session_cookie,
)
from sleeper_manager.cloudflare.manager_context import ManagerContext
from sleeper_manager.cloudflare.manager_routes import manage
from tests.sleeper_manager.cloudflare.test_confirmation import request
from tests.sleeper_manager.persistence.test_manager_store import KEY, NOW, row
from tests.sleeper_manager.persistence.test_manager_store import store as store

SECRET = "test-owner-key-with-at-least-32-characters"
CHANGE_ID = "a" * 32


def owner_request(path="/manage", *, method="GET", values=None, session=None):  # type: ignore[no-untyped-def]
    req = request(method=method, values=values)
    req.url = "https://worker.test" + path
    session = session or issue_session(SECRET, NOW)
    req.headers["cookie"] = f"{COOKIE_NAME}={session}"
    return req


@pytest.fixture
def environment(store, monkeypatch):  # type: ignore[no-untyped-def]
    context = ManagerContext(
        "league", 1, 1, ("PF",), ("player",), {"player": "Player name"}, "America/Chicago"
    )

    async def load(*args):  # type: ignore[no-untyped-def]
        return context

    monkeypatch.setattr("sleeper_manager.cloudflare.manager_routes.load_manager_context", load)
    return SimpleNamespace(MANAGER_ACCESS_KEY=SECRET, sleeper_manager_state=store._database)


def save_request(**overrides):  # type: ignore[no-untyped-def]
    session = issue_session(SECRET, NOW)
    values = {
        "csrf": csrf_proof(SECRET, session),
        "player_id": "player",
        "game_id": "game",
        "version": "0",
        "change_id": CHANGE_ID,
        "action": "not_locked",
        **overrides,
    }
    return owner_request("/manage/save", method="POST", values=values, session=session)


def test_public_access_gets_login_without_private_data(environment):  # type: ignore[no-untyped-def]
    req = request()
    req.url = "https://worker.test/manage"
    response = asyncio.run(manage(req, environment, None, now=NOW))
    assert "Owner access" in response.body
    assert "Player name" not in response.body and "32.80" not in response.body
    assert "set-cookie" not in response.headers
    assert response.headers["referrer-policy"] == "same-origin"


@pytest.mark.parametrize("origin", [None, "null", "https://evil.test"])
def test_login_rejects_untrusted_origin_even_with_valid_key(environment, origin):  # type: ignore[no-untyped-def]
    login = owner_request("/manage/login", method="POST", values={"password": SECRET})
    login.headers["origin"] = origin
    response = asyncio.run(manage(login, environment, None, now=NOW))
    assert response.status == 403
    assert "set-cookie" not in response.headers
    assert "Request rejected" in response.body


def test_login_issues_secure_cookie_and_logout_clears_it(environment):  # type: ignore[no-untyped-def]
    login = owner_request("/manage/login", method="POST", values={"password": SECRET})
    response = asyncio.run(manage(login, environment, None, now=NOW))
    assert response.status == 303
    cookie = response.headers["set-cookie"]
    for flag in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/manage"):
        assert flag in cookie
    session = cookie.split(";", 1)[0].split("=", 1)[1]
    logout = owner_request(
        "/manage/logout",
        method="POST",
        values={"csrf": csrf_proof(SECRET, session)},
        session=session,
    )
    response = asyncio.run(manage(logout, environment, None, now=NOW))
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_save_corrects_false_lock_and_shows_replan_pending(environment, store):  # type: ignore[no-untyped-def]
    response = asyncio.run(manage(save_request(), environment, None, now=NOW))
    assert response.status == 303
    assert row(store).acknowledged_action is None
    editor = asyncio.run(manage(owner_request(), environment, None, now=NOW))
    assert "Recorded: <strong>Wait</strong>" in editor.body
    assert "A new plan is queued or running" in editor.body
    assert "Correction history" in editor.body
    assert "32.80 pts" in editor.body
    assert '<form method="post" action="/manage/save">' in editor.body


@pytest.mark.parametrize(
    "values,status",
    [
        ({"csrf": "bad"}, 403),
        ({"version": "5"}, 409),
        ({"player_id": "other"}, 404),
        ({"action": "unknown"}, 400),
        ({"change_id": "bad"}, 400),
    ],
)
def test_invalid_or_stale_edits_write_nothing(environment, store, values, status):  # type: ignore[no-untyped-def]
    response = asyncio.run(manage(save_request(**values), environment, None, now=NOW))
    assert response.status == status
    assert row(store).acknowledged_action == "locked"
    assert asyncio.run(store.decision_history("league", 1, 1)) == []


def test_cross_roster_cannot_be_selected_by_submitted_fields(environment, store):  # type: ignore[no-untyped-def]
    other_key = replace(KEY, roster_id=2, player_id="other")
    asyncio.run(store.upsert_lock_in_opportunity(replace(row(store), key=other_key)))
    response = asyncio.run(
        manage(save_request(player_id="other", roster_id="2"), environment, None, now=NOW)
    )
    assert response.status == 404


def test_get_save_and_cross_origin_post_are_non_mutating(environment, store):  # type: ignore[no-untyped-def]
    response = asyncio.run(manage(owner_request("/manage/save"), environment, None, now=NOW))
    assert response.status == 405
    req = save_request()
    req.headers["origin"] = "https://evil.test"
    response = asyncio.run(manage(req, environment, None, now=NOW))
    assert response.status == 403
    assert row(store).acknowledged_action == "locked"


def test_queue_failure_preserves_saved_decision_and_durable_work(environment, store):  # type: ignore[no-untyped-def]
    async def fail(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("queue unavailable")

    environment.PLANNING_QUEUE = SimpleNamespace(send=fail)
    assert asyncio.run(manage(save_request(), environment, None, now=NOW)).status == 303
    assert row(store).acknowledged_action is None
    assert asyncio.run(store.has_pending_manager_plan(NOW))


def test_owner_cookie_rejects_tampering_expiry_and_rotation():
    session = issue_session(SECRET, NOW)
    cookie = f"{COOKIE_NAME}={session}"
    assert read_session(cookie, SECRET, NOW) == session
    assert read_session(cookie, SECRET + "rotated", NOW) is None
    assert read_session(cookie, SECRET, NOW + timedelta(seconds=SESSION_SECONDS)) is None
    assert read_session(f"{COOKIE_NAME}={session}altered", SECRET, NOW) is None
    assert read_session(session_cookie(""), SECRET, NOW) is None


def test_public_home_hides_old_advice_while_correction_is_pending(store):  # type: ignore[no-untyped-def]
    from sleeper_manager.cloudflare.home import _recommendation
    from sleeper_manager.workflows.manager_decisions import ManagerAction

    asyncio.run(
        store.save_decision(
            row(store),
            ManagerAction.NOT_LOCKED,
            change_id=CHANGE_ID,
            now=NOW,
            actual_slot=(0, "PF"),
        )
    )
    result = asyncio.run(_recommendation(store, "league", 1, now=NOW))
    assert result[2] == "Updating your plan"
    assert "queued or running" in result[3]


def test_worker_routes_owner_login_without_reading_private_state(environment):  # type: ignore[no-untyped-def]
    from sleeper_manager.cloudflare.worker import Default

    worker = Default()
    worker.env = environment
    req = request()
    req.url = "https://worker.test/manage"
    response = asyncio.run(worker.fetch(req))
    assert response.status_code == 200
    assert "Owner access" in response.body
    assert "Player name" not in response.body


def test_expired_access_key_and_bad_login_reveal_no_decisions(environment):  # type: ignore[no-untyped-def]
    bad = owner_request("/manage/login", method="POST", values={"password": "wrong"})
    response = asyncio.run(manage(bad, environment, None, now=NOW))
    assert response.status == 403 and "Player name" not in response.body
    environment.MANAGER_ACCESS_KEY = ""
    assert asyncio.run(manage(owner_request(), environment, None, now=NOW)).status == 503


def batch_request(action="not_locked", **overrides):  # type: ignore[no-untyped-def]
    session = issue_session(SECRET, NOW)
    return owner_request(
        "/manage/save",
        method="POST",
        session=session,
        values={
            "csrf": csrf_proof(SECRET, session),
            "batch_id": CHANGE_ID,
            "count": "1",
            "player_id_0": "player",
            "game_id_0": "game",
            "version_0": "0",
            "action_0": action,
            **overrides,
        },
    )


def test_shared_form_saves_changed_actions_and_queues_one_plan(environment, store):  # type: ignore[no-untyped-def]
    response = asyncio.run(manage(batch_request(), environment, None, now=NOW))
    assert response.status == 303
    assert row(store).acknowledged_action is None
    assert len(asyncio.run(store.list_scheduled_work())) == 1


def test_unchanged_shared_form_does_not_enqueue_or_audit(environment, store):  # type: ignore[no-untyped-def]
    response = asyncio.run(manage(batch_request("locked"), environment, None, now=NOW))
    assert response.status == 303
    assert asyncio.run(store.list_scheduled_work()) == ()
    assert asyncio.run(store.decision_history("league", 1, 1)) == []


@pytest.mark.parametrize(
    "fields,status",
    [
        ({"version_0": "9"}, 409),
        ({"count": "17"}, 400),
        ({"player_id_0": "other"}, 400),
        ({"action_0": "invalid"}, 400),
        ({"csrf": "wrong"}, 403),
    ],
)
def test_invalid_batch_form_changes_nothing(environment, store, fields, status):  # type: ignore[no-untyped-def]
    assert asyncio.run(manage(batch_request(**fields), environment, None, now=NOW)).status == status
    assert row(store).acknowledged_action == "locked"
    assert asyncio.run(store.list_scheduled_work()) == ()


def test_retry_plan_preserves_decisions_and_is_idempotent(environment, store):  # type: ignore[no-untyped-def]
    session = issue_session(SECRET, NOW)
    req = owner_request(
        "/manage/replan",
        method="POST",
        session=session,
        values={
            "csrf": csrf_proof(SECRET, session),
            "request_id": CHANGE_ID,
        },
    )
    assert asyncio.run(manage(req, environment, None, now=NOW)).status == 303
    assert asyncio.run(manage(req, environment, None, now=NOW)).status == 303
    assert len(asyncio.run(store.list_scheduled_work())) == 1
    assert row(store).acknowledged_action == "locked"
    assert asyncio.run(store.decision_history("league", 1, 1)) == []
