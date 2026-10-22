"""Owner controls follow the public dashboard's restrained dark, monospace layout."""

import json
from datetime import datetime
from html import escape
from uuid import uuid4
from zoneinfo import ZoneInfo

from sleeper_manager.cloudflare.form_pages import FormPage, hidden, page
from sleeper_manager.cloudflare.manager_context import ManagerContext
from sleeper_manager.persistence.base import RecommendationRecord
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityRecord

LABELS = {"not_locked": "Not locked yet", "locked": "Locked in Sleeper", "passed": "Passed"}


def login_page(*, rejected: bool = False) -> FormPage:
    """The password is entered locally and is never echoed into HTML or URLs."""
    return page(
        "Manage decisions",
        ('<p class="notice">Access key not accepted.</p>' if rejected else "")
        + '<section><p>Owner access</p><form method="post" action="/manage/login">'
        '<label>Access key <input type="password" name="password" required '
        'autocomplete="current-password" maxlength="256"></label>'
        '<button type="submit">Continue</button></form></section>',
        status=403 if rejected else 200,
    )


def _moment(value: datetime, context: ManagerContext) -> str:
    return value.astimezone(ZoneInfo(context.timezone)).strftime("%a %b %d, %I:%M %p %Z")


def decision_card(
    record: LockInOpportunityRecord, context: ManagerContext, csrf: str, reported: str | None
) -> str:
    """A game's score is observed evidence, never an editable input."""
    key = record.key
    name = context.names.get(key.player_id, key.player_id)
    action = record.acknowledged_action or reported
    current = LABELS.get(action or "", "Not reported")
    options = "".join(
        f'<option value="{value}"{" selected" if value == action else ""}>{label}</option>'
        for value, label in LABELS.items()
    )
    score = (
        "Score not verified yet"
        if record.stable_score is None
        else f"{record.stable_score:.2f} pts"
    )
    slot = context.actual_slot(key.player_id)
    placement = (
        f"Current slot: {slot[1]} (#{slot[0] + 1})" if slot else "Currently on bench/reserve"
    )
    return (
        f"<section><h2>{escape(name)}</h2><p>{escape(_moment(record.scheduled_start, context))}"
        f" · game {escape(key.game_id)}</p><p>{score} · {escape(placement)}</p>"
        f"<p>Recorded status: <strong>{current}</strong></p>"
        '<form method="post" action="/manage/save">'
        + hidden("csrf", csrf)
        + hidden("player_id", key.player_id)
        + hidden("game_id", key.game_id)
        + hidden("version", record.row_version)
        + hidden("change_id", uuid4().hex)
        + f'<label>What have you done in Sleeper? <select name="action">{options}</select></label>'
        '<button type="submit">Save and update plan</button></form></section>'
    )


def editor_page(
    context: ManagerContext,
    records: tuple[LockInOpportunityRecord, ...],
    history: list[dict[str, object]],
    csrf: str,
    *,
    now: datetime,
    pending: bool,
    plan: RecommendationRecord | None,
) -> FormPage:
    """Keep recorded actions separate from advice and expose previous values for corrections."""
    latest = {
        (str(row["player_id"]), str(row["game_id"])): str(row["action"])
        for row in reversed(history)
    }
    if pending:
        summary = '<p class="notice">Decision saved. A new plan is queued or running. '
        summary += '<a href="/manage">Refresh to check the result</a>.</p>'
    elif plan:
        summary = (
            f"<section><h2>{escape(plan.title)}</h2><pre>{escape(plan.message)}</pre></section>"
        )
    else:
        summary = "<p>A new plan has not completed yet.</p>"
    cards = "".join(
        decision_card(row, context, csrf, latest.get((row.key.player_id, row.key.game_id)))
        for row in records
        if row.scheduled_start <= now or row.acknowledged_action
    )
    entries = []
    for row in history:
        previous = json.loads(str(row["previous_state_json"]))
        previous_action = previous.get("acknowledged_action") or "not_locked"
        label = context.names.get(str(row["player_id"]), str(row["player_id"]))
        moment = _moment(datetime.fromisoformat(str(row["recorded_at"])), context)
        entries.append(
            f"<li>{escape(label)} · game {escape(str(row['game_id']))}<br>"
            f"{escape(LABELS.get(previous_action, previous_action))} → "
            f"{LABELS[str(row['action'])]}<br><small>{escape(moment)}</small></li>"
        )
    return page(
        f"Your decisions · week {context.week}",
        "<p>Record only actions you have already performed in Sleeper. "
        "Saving here updates the assistant's plan; it does not change Sleeper.</p>"
        + summary
        + (cards or "<section>No completed games to review yet.</section>")
        + "<details><summary>Correction history</summary><ul>"
        + ("".join(entries) or "<li>No manual corrections yet.</li>")
        + '</ul></details><form method="post" action="/manage/logout">'
        + hidden("csrf", csrf)
        + '<button type="submit">Sign out</button></form>',
    )
