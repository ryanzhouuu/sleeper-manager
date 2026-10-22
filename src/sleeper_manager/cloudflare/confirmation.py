"""Notification URLs display intent on GET and require an explicit same-origin POST."""

from datetime import datetime
from html import escape
from typing import Any

from sleeper_manager.cloudflare.form_pages import (
    FormPage,
    form_values,
    hidden,
    page,
    same_origin,
)
from sleeper_manager.cloudflare.planning_signal import publish_planning_wake
from sleeper_manager.cloudflare.routes import acknowledge, parse_action
from sleeper_manager.persistence.manager_store import D1ManagerStore
from sleeper_manager.persistence.tokens import hash_action_token


async def confirmation(
    request: Any, store: D1ManagerStore, values: dict[str, str], *, now: datetime, env: Any = None
) -> FormPage:
    """Opening or previewing a URL cannot record a Sleeper action."""
    if request.method == "POST":
        if not same_origin(request):
            return page(
                "Confirmation rejected", "<p>Open the confirmation link again.</p>", status=403
            )
        try:
            values = await form_values(request)
        except (ValueError, UnicodeError):
            return page("Invalid form", "<p>Open the confirmation link again.</p>", status=400)
        raw = values.get("token", "")
        prior = await store.preview_token(hash_action_token(raw)) if 0 < len(raw) <= 256 else None
        if prior is not None and prior["status"] == "superseded":
            return page(
                "Confirmation replaced",
                '<p><a href="/manage">Review current decisions</a></p>',
                status=409,
            )
        result = await acknowledge(store, values, now=now)
        saved = result.payload["status"] in {"acknowledged", "already_used"}
        if saved:
            record = await store.preview_token(hash_action_token(values.get("token", "")))
            if record is not None:
                await store.ensure_ack_replan(str(record["recommendation_id"]), now)
                if env is not None:
                    await publish_planning_wake(env, now)
        message = "Your decision is recorded." if saved else "This confirmation is unavailable."
        return page(
            "Decision saved" if saved else "Confirmation unavailable",
            f'<p>{message}</p><p><a href="/manage">Review or correct decisions</a></p>',
            status=result.status_code,
        )
    if request.method != "GET":
        return page("Method not allowed", "", status=405)
    token = values.get("token", "")
    action = parse_action(values.get("action"))
    if not token or len(token) > 256 or action is None:
        return page("Invalid confirmation", "<p>Use the link in your notification.</p>", status=400)
    record = await store.preview_token(hash_action_token(token))
    if record is None or record["action"] != action.value:
        return page("Confirmation unavailable", "<p>This link is invalid.</p>", status=404)
    if record["status"] == "superseded":
        return page(
            "Confirmation replaced",
            '<p><a href="/manage">Review current decisions</a></p>',
            status=409,
        )
    if record["used_at"] is not None:
        return page("Already recorded", '<p><a href="/manage">Review or correct decisions</a></p>')
    if datetime.fromisoformat(str(record["expires_at"])) <= now:
        return page(
            "Confirmation expired", '<p><a href="/manage">Review decisions</a></p>', status=410
        )
    if record["status"] != "pending":
        return page("Confirmation replaced", "<p>Use your latest recommendation.</p>", status=409)
    label = "I locked this game in Sleeper" if action.value == "locked" else "I passed this game"
    return page(
        "Confirm your Sleeper action",
        f"<section><h2>{escape(str(record['title']))}</h2>"
        f"<pre>{escape(str(record['message']))}</pre></section>"
        "<p>Save only after performing this action in Sleeper. "
        "This page does not change your Sleeper lineup.</p>"
        '<form method="post" action="/ack">'
        + hidden("token", token)
        + hidden("action", action.value)
        + f'<button type="submit">{label}</button></form>'
        '<p><a href="/manage">Review or correct decisions</a></p>',
    )
