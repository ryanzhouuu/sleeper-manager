"""Owner identity, CSRF and immutable game scope protect dashboard edits."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from sleeper_manager.cloudflare.manager_auth import (
    COOKIE_NAME,
    SESSION_SECONDS,
    csrf_proof,
    issue_session,
    password_matches,
    read_session,
    session_cookie,
)
from sleeper_manager.cloudflare.manager_context import ManagerContext
from sleeper_manager.cloudflare.manager_routes import manage
from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from tests.sleeper_manager.cloudflare.form_request_support import request
from tests.sleeper_manager.persistence.test_manager_store import (
    NOW,
    _with_owner_actions,
    row,
)
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


def test_get_manage_reports_signed_out_session_without_private_data(environment):  # type: ignore[no-untyped-def]
    req = request()
    req.url = "https://worker.test/manage"
    response = asyncio.run(manage(req, environment, None, now=NOW))
    assert response.status == 200
    assert response.data == {"status": "session", "signed_in": False, "csrf": None}
    assert "Player name" not in response.body and "32.80" not in response.body
    assert "set-cookie" not in response.headers
    assert response.headers["referrer-policy"] == "same-origin"


def test_get_manage_returns_a_csrf_proof_for_the_signed_in_owner(environment):  # type: ignore[no-untyped-def]
    session = issue_session(SECRET, NOW)
    response = asyncio.run(manage(owner_request(session=session), environment, None, now=NOW))
    assert response.data == {
        "status": "session",
        "signed_in": True,
        "csrf": csrf_proof(SECRET, session),
    }


@pytest.mark.parametrize("origin", [None, "null", "https://evil.test"])
def test_login_rejects_untrusted_origin_even_with_valid_key(environment, origin):  # type: ignore[no-untyped-def]
    login = owner_request("/manage/login", method="POST", values={"password": SECRET})
    login.headers["origin"] = origin
    response = asyncio.run(manage(login, environment, None, now=NOW))
    assert response.status == 403
    assert "set-cookie" not in response.headers
    assert "Request rejected" in response.body


@pytest.mark.parametrize("secret", ["x", "short-owner-key", SECRET])
def test_login_issues_secure_cookie_and_logout_clears_it(environment, secret):  # type: ignore[no-untyped-def]
    environment.MANAGER_ACCESS_KEY = secret
    public = request()
    public.url = "https://worker.test/manage"
    assert asyncio.run(manage(public, environment, None, now=NOW)).status == 200
    login = owner_request("/manage/login", method="POST", values={"password": secret})
    response = asyncio.run(manage(login, environment, None, now=NOW))
    assert response.status == 303
    cookie = response.headers["set-cookie"]
    for flag in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/manage"):
        assert flag in cookie
    session = cookie.split(";", 1)[0].split("=", 1)[1]
    logout = owner_request(
        "/manage/logout",
        method="POST",
        values={"csrf": csrf_proof(secret, session)},
        session=session,
    )
    response = asyncio.run(manage(logout, environment, None, now=NOW))
    assert "Max-Age=0" in response.headers["set-cookie"]


@pytest.mark.parametrize("secret", ["x", "short-owner-key", SECRET])
def test_owner_cookie_rejects_tampering_expiry_and_rotation(secret):  # type: ignore[no-untyped-def]
    session = issue_session(secret, NOW)
    cookie = f"{COOKIE_NAME}={session}"
    assert read_session(cookie, secret, NOW) == session
    assert read_session(cookie, secret + "rotated", NOW) is None
    assert read_session(cookie, secret, NOW + timedelta(seconds=SESSION_SECONDS)) is None
    assert read_session(f"{COOKIE_NAME}={session}altered", secret, NOW) is None
    assert read_session(session_cookie(""), secret, NOW) is None


@pytest.mark.parametrize("secret", ["x", "short-owner-key", SECRET])
def test_access_key_requires_exact_nonempty_match(secret):  # type: ignore[no-untyped-def]
    assert password_matches(secret, secret)
    assert not password_matches(secret + "wrong", secret)
    assert not password_matches("", secret)
    assert not password_matches("", "")
    assert read_session(session_cookie(issue_session("", NOW)), "", NOW) is None


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
    assert "Player name" not in str(response.body)


def test_expired_access_key_and_bad_login_reveal_no_decisions(environment):  # type: ignore[no-untyped-def]
    bad = owner_request("/manage/login", method="POST", values={"password": "wrong"})
    response = asyncio.run(manage(bad, environment, None, now=NOW))
    assert response.status == 403 and "Player name" not in response.body
    environment.MANAGER_ACCESS_KEY = ""
    assert asyncio.run(manage(owner_request(), environment, None, now=NOW)).status == 503


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


def _unverified(store, **changes):  # type: ignore[no-untyped-def]
    _with_owner_actions(store)
    fields = {
        "status": LockInOpportunityStatus.INELIGIBLE,
        "rostered_at_tipoff": False,
        "tipoff_unverified": True,
        "acknowledged_action": None,
        "acknowledged_at": None,
        **changes,
    }
    current = replace(row(store), **fields)
    asyncio.run(store.update_lock_in_opportunity(current, expected_version=current.row_version))
    return row(store)


def override_request(store, *, session=None, **overrides):  # type: ignore[no-untyped-def]
    session = session or issue_session(SECRET, NOW)
    values = {
        "csrf": csrf_proof(SECRET, session),
        "player_id": "player",
        "game_id": "game",
        "version": str(row(store).row_version),
        "action": "lockable",
        **overrides,
    }
    return owner_request("/manage/override", method="POST", values=values, session=session)


def test_override_requires_session(environment, store):  # type: ignore[no-untyped-def]
    before = _unverified(store)
    req = override_request(store)
    req.headers["cookie"] = ""

    response = asyncio.run(manage(req, environment, None, now=NOW))

    assert response.status == 403
    assert row(store) == before


def test_override_rejects_stale_csrf(environment, store):  # type: ignore[no-untyped-def]
    before = _unverified(store)
    stale = csrf_proof(SECRET, issue_session(SECRET, NOW - timedelta(hours=1)))

    response = asyncio.run(manage(override_request(store, csrf=stale), environment, None, now=NOW))

    assert response.status == 403
    assert row(store) == before
    assert asyncio.run(store.list_scheduled_work()) == ()


def test_override_marks_unverified_game_lockable_and_wakes_planning(environment, store):  # type: ignore[no-untyped-def]
    _unverified(store)
    sent: list[str] = []

    class Queue:
        async def send(self, body: str, contentType: str) -> None:  # noqa: N803
            sent.append(body)

    environment.PLANNING_QUEUE = Queue()

    response = asyncio.run(manage(override_request(store), environment, None, now=NOW))

    assert response.status == 303
    assert response.headers["location"] == "/"
    updated = row(store)
    assert updated.rostered_at_tipoff is True
    assert updated.tipoff_unverified is False
    assert len(sent) == 1


def test_override_marks_unverified_game_not_lockable(environment, store):  # type: ignore[no-untyped-def]
    _unverified(store, status=LockInOpportunityStatus.ACTIONABLE, rostered_at_tipoff=True)

    req = override_request(store, action="not_lockable")
    response = asyncio.run(manage(req, environment, None, now=NOW))

    assert response.status == 303
    assert row(store).status is LockInOpportunityStatus.INELIGIBLE
    assert row(store).tipoff_unverified is False


def test_override_refuses_sleeper_locked_row(environment, store):  # type: ignore[no-untyped-def]
    before = _unverified(
        store,
        status=LockInOpportunityStatus.ACKNOWLEDGED_LOCKED,
        trace_json=json.dumps({"lock_source": "sleeper"}),
    )

    response = asyncio.run(manage(override_request(store), environment, None, now=NOW))

    assert response.status == 400
    assert row(store) == before


@pytest.mark.parametrize(
    ("overrides", "status"),
    [
        ({"action": "locked"}, 400),
        ({"version": "x"}, 400),
        ({"version": "99"}, 409),
        ({"game_id": "missing"}, 404),
    ],
)
def test_invalid_overrides_change_nothing(environment, store, overrides, status):  # type: ignore[no-untyped-def]
    before = _unverified(store)

    response = asyncio.run(manage(override_request(store, **overrides), environment, None, now=NOW))

    assert response.status == status
    assert row(store) == before


def test_override_refuses_a_verified_game(environment, store):  # type: ignore[no-untyped-def]
    before = _unverified(store, tipoff_unverified=False)

    response = asyncio.run(manage(override_request(store), environment, None, now=NOW))

    assert response.status == 400
    assert row(store) == before


@pytest.mark.parametrize("path", ["/manage/save", "/manage/edit"])
def test_save_and_editor_paths_are_gone(environment, store, path):  # type: ignore[no-untyped-def]
    session = issue_session(SECRET, NOW)
    values = {"csrf": csrf_proof(SECRET, session), "batch_id": CHANGE_ID}
    req = owner_request(path, method="POST", values=values, session=session)

    assert asyncio.run(manage(req, environment, None, now=NOW)).status == 404
