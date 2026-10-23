"""Authenticated owner forms save audited decisions and signal the existing planning queue."""

from dataclasses import replace
from datetime import datetime
from html import escape
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

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
from sleeper_manager.cloudflare.manager_submission import save_manager_form
from sleeper_manager.cloudflare.manager_views import editor_page, login_page
from sleeper_manager.cloudflare.planning_signal import publish_planning_wake
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityKey
from sleeper_manager.persistence.manager_store import D1ManagerStore
from sleeper_manager.workflows.manager_decisions import ManagerAction


def _redirect(location: str, cookie: str | None = None) -> FormPage:
    result = page("Saved", "", status=303)
    headers = {**result.headers, "location": location}
    if cookie is not None:
        headers["set-cookie"] = cookie
    return replace(result, headers=headers)


async def manage(request: Any, env: Any, fetcher: Any, *, now: datetime) -> FormPage:
    """Public access reveals only a login form; edits are owner-scoped and protected by CSRF."""
    path = urlparse(str(request.url)).path
    if path not in {"/manage", "/manage/login", "/manage/save", "/manage/logout", "/manage/replan"}:
        return page("Not found", "", status=404)
    if request.method not in {"GET", "POST"}:
        return page("Method not allowed", "", status=405)
    secret = str(getattr(env, "MANAGER_ACCESS_KEY", ""))
    if len(secret) < 32:
        return page(
            "Owner controls unavailable", "<p>Owner access has not been configured.</p>", status=503
        )
    values: dict[str, str] = {}
    if request.method == "POST":
        if not same_origin(request):
            return page("Request rejected", "<p>Open the owner controls again.</p>", status=403)
        try:
            values = await form_values(request)
        except (ValueError, UnicodeError):
            return page("Invalid form", "<p>Reload the page and try again.</p>", status=400)
        if path == "/manage/login":
            if not password_matches(values.get("password", ""), secret):
                return login_page(rejected=True)
            return _redirect("/manage", session_cookie(issue_session(secret, now)))
    session = read_session(str(request.headers.get("cookie") or ""), secret, now)
    if session is None:
        return login_page()
    if request.method == "GET" and path != "/manage":
        return page("Method not allowed", "", status=405)
    if request.method == "POST":
        if not valid_csrf(values.get("csrf", ""), secret, session):
            return page("Session changed", "<p>Reload the page and try again.</p>", status=403)
        if path == "/manage/logout":
            return _redirect("/manage", session_cookie("", expired=True))
        if path == "/manage/replan":
            try:
                request_id = UUID(values["request_id"]).hex
            except (ValueError, KeyError):
                return page("Invalid retry", "<p>Reload the owner controls.</p>", status=400)
            await D1ManagerStore(env.sleeper_manager_state).retry_manager_plan(request_id, now)
            await publish_planning_wake(env, now)
            return _redirect("/manage")
        if path != "/manage/save":
            return page("Method not allowed", "", status=405)
    store = D1ManagerStore(env.sleeper_manager_state)
    try:
        context = await load_manager_context(env, fetcher, store, now)
    except Exception as error:
        print(f"Manager context unavailable: {type(error).__name__}")
        return page(
            "League unavailable",
            "<p>Reload to try again. No decisions were changed.</p>",
            status=503,
        )
    if request.method == "POST":
        if "batch_id" in values:
            try:
                saved, changed = await save_manager_form(values, store, context, now)
            except (KeyError, ValueError) as error:
                return page(
                    "Decision not saved",
                    f"<p>{escape(str(error))}</p>",
                    status=400,
                    data={
                        "status": "message",
                        "title": "Decision not saved",
                        "message": str(error),
                    },
                )
            if not saved:
                return page(
                    "Decision changed", "<p>Reload and review the latest status.</p>", status=409
                )
            if changed:
                await publish_planning_wake(env, now)
            return _redirect("/manage")
        try:
            action = ManagerAction(values.get("action", ""))
            change_id = UUID(values["change_id"]).hex
            version = int(values["version"])
            if version < 0:
                raise ValueError("Invalid version")
            key = LockInOpportunityKey(
                context.league_id,
                context.week,
                context.roster_id,
                values["player_id"],
                values["game_id"],
            )
            current = await store.get_lock_in_opportunity(key)
            if current is None:
                return page("Game unavailable", "<p>Reload the owner controls.</p>", status=404)
            saved = await store.save_decision(
                replace(current, row_version=version),
                action,
                change_id=change_id,
                now=now,
                actual_slot=context.actual_slot(key.player_id),
            )
        except (KeyError, ValueError) as error:
            return page(
                "Decision not saved",
                f"<p>{escape(str(error))}</p>",
                status=400,
                data={"status": "message", "title": "Decision not saved", "message": str(error)},
            )
        if not saved:
            return page(
                "Decision changed",
                "<p>Reload and review the latest status before saving.</p>",
                status=409,
            )
        await publish_planning_wake(env, now)
        return _redirect("/manage")
    records = await store.list_week_lock_in_opportunities(context.league_id, context.week)
    owned = tuple(row for row in records if row.key.roster_id == context.roster_id)
    history = await store.decision_history(context.league_id, context.week, context.roster_id)
    plan = await store.latest_current_recommendation(
        context.league_id, context.week, decision_type="weekly_lineup"
    )
    if plan is not None and plan.deadline is not None and plan.deadline <= now:
        plan = None
    pending = plan is None and await store.has_pending_manager_plan(now)
    return editor_page(
        context, owned, history, csrf_proof(secret, session), now=now, pending=pending, plan=plan
    )
