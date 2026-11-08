"""Owner session routes and the one manual input: answering an unverified tipoff.

Locks and passes are read from Sleeper, so there is no decision editor.
"""

from dataclasses import replace
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

from sleeper_manager.cloudflare.form_pages import FormPage, form_values, page, same_origin
from sleeper_manager.cloudflare.manager_auth import (
    csrf_proof,
    issue_session,
    password_matches,
    read_session,
    session_cookie,
    valid_csrf,
)
from sleeper_manager.cloudflare.manager_context import load_manager_context
from sleeper_manager.cloudflare.planning_signal import publish_planning_wake
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityKey
from sleeper_manager.persistence.manager_store import D1ManagerStore
from sleeper_manager.workflows.manager_decisions import ManagerAction, ManagerEdit

_PATHS = frozenset(
    {"/manage", "/manage/login", "/manage/logout", "/manage/replan", "/manage/override"}
)
_OVERRIDES = frozenset({ManagerAction.LOCKABLE, ManagerAction.NOT_LOCKABLE})
_DASHBOARD = "/"


def _redirect(location: str, cookie: str | None = None) -> FormPage:
    result = page("Saved", "", status=303)
    headers = {**result.headers, "location": location}
    if cookie is not None:
        headers["set-cookie"] = cookie
    return replace(result, headers=headers)


def _message(title: str, status: int, message: str = "") -> FormPage:
    data: dict[str, object] = {"status": "message", "title": title}
    if message:
        data["message"] = message
    return page(title, f"<p>{message}</p>" if message else "", status=status, data=data)


def login_page(*, rejected: bool = False) -> FormPage:
    """The password is entered locally and is never echoed into HTML or URLs."""
    return page(
        "Owner access",
        ('<p class="notice">Access key not accepted.</p>' if rejected else "")
        + '<section><form method="post" action="/manage/login">'
        '<label>Access key <input type="password" name="password" required '
        'autocomplete="current-password" maxlength="256"></label>'
        '<button type="submit">Continue</button></form></section>',
        status=403 if rejected else 200,
        data={"status": "login", "rejected": rejected},
    )


async def manage(request: Any, env: Any, fetcher: Any, *, now: datetime) -> FormPage:
    """GET reports the session; every POST except login needs the session and its CSRF proof."""
    path = urlparse(str(request.url)).path
    if path not in _PATHS:
        return page("Not found", "", status=404)
    if request.method not in {"GET", "POST"}:
        return page("Method not allowed", "", status=405)
    secret = str(getattr(env, "MANAGER_ACCESS_KEY", ""))
    if not secret:
        return _message("Owner controls unavailable", 503, "Owner access has not been configured.")
    session = read_session(str(request.headers.get("cookie") or ""), secret, now)
    if request.method == "GET":
        if path != "/manage":
            return page("Method not allowed", "", status=405)
        return page(
            "Owner session",
            "",
            data={
                "status": "session",
                "signed_in": session is not None,
                "csrf": None if session is None else csrf_proof(secret, session),
            },
        )
    if not same_origin(request):
        return _message("Request rejected", 403, "Open the dashboard again.")
    try:
        values = await form_values(request)
    except (ValueError, UnicodeError):
        return _message("Invalid form", 400, "Reload the page and try again.")
    if path == "/manage/login":
        if not password_matches(values.get("password", ""), secret):
            return login_page(rejected=True)
        return _redirect(_DASHBOARD, session_cookie(issue_session(secret, now)))
    if session is None or not valid_csrf(values.get("csrf", ""), secret, session):
        return _message("Session changed", 403, "Sign in again and retry.")
    if path == "/manage/logout":
        return _redirect(_DASHBOARD, session_cookie("", expired=True))
    store = D1ManagerStore(env.sleeper_manager_state)
    if path == "/manage/replan":
        try:
            request_id = UUID(values["request_id"]).hex
        except (ValueError, KeyError):
            return _message("Invalid retry", 400, "Reload the dashboard.")
        await store.retry_manager_plan(request_id, now)
        await publish_planning_wake(env, now)
        return _redirect(_DASHBOARD)
    return await _override(values, env, fetcher, store, now=now)


async def _override(
    values: dict[str, str], env: Any, fetcher: Any, store: D1ManagerStore, *, now: datetime
) -> FormPage:
    """Record whether an unverified starter was in a starting slot at tipoff, then replan."""
    try:
        action = ManagerAction(values.get("action", ""))
        version = int(values["version"])
        if action not in _OVERRIDES or version < 0:
            raise ValueError("Invalid override")
        player_id, game_id = values["player_id"], values["game_id"]
    except (KeyError, ValueError):
        return _message("Override not saved", 400, "Reload the dashboard and try again.")
    try:
        context = await load_manager_context(env, fetcher, store, now)
    except Exception as error:
        print(f"Manager context unavailable: {type(error).__name__}")
        return _message("League unavailable", 503, "Reload to try again. Nothing was changed.")
    key = LockInOpportunityKey(
        context.league_id, context.week, context.roster_id, player_id, game_id
    )
    current = await store.get_lock_in_opportunity(key)
    if current is None:
        return _message("Game unavailable", 404, "Reload the dashboard.")
    if current.row_version != version:
        return _message("Game changed", 409, "Reload and review the latest status.")
    try:
        saved = await store.save_decisions(
            (ManagerEdit(current, action, None),), batch_id=uuid4().hex, now=now
        )
    except ValueError as error:
        return _message("Override not saved", 400, str(error))
    if not saved:
        return _message("Game changed", 409, "Reload and review the latest status.")
    await publish_planning_wake(env, now)
    return _redirect(_DASHBOARD)
