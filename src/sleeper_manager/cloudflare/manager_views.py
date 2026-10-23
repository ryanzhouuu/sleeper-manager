"""Owner controls follow the public dashboard's restrained dark, monospace layout."""

import json
from datetime import datetime
from html import escape
from uuid import uuid4
from zoneinfo import ZoneInfo

from sleeper_manager.cloudflare.form_pages import FormPage, hidden, page
from sleeper_manager.cloudflare.manager_context import ManagerContext
from sleeper_manager.cloudflare.manager_games import (
    lockable_games,
    recommendation_for,
    recorded_state,
)
from sleeper_manager.persistence.base import RecommendationRecord
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityRecord

LABELS = {"not_locked": "Wait", "locked": "Locked in Sleeper", "passed": "Passed"}


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
    record: LockInOpportunityRecord,
    context: ManagerContext,
    index: int,
    *,
    now: datetime,
    plan: RecommendationRecord | None,
) -> str:
    """A game's score is observed evidence, never an editable input."""
    key = record.key
    name = context.names.get(key.player_id, key.player_id)
    action = record.acknowledged_action or "not_locked"
    options = "".join(
        f'<label class="choice"><input type="radio" name="action_{index}" '
        f'value="{value}"{" checked" if value == action else ""}>'
        f"{'&#128274; ' if value == 'locked' else ''}{label}</label>"
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
        f"<p>Lock window ends: {escape(_moment(record.action_deadline, context))}</p>"
        f"<p>Advisor: <strong>{recommendation_for(record, plan, now)}</strong><br>"
        f"Recorded: <strong>{recorded_state(record, now)}</strong></p>"
        + hidden(f"player_id_{index}", key.player_id)
        + hidden(f"game_id_{index}", key.game_id)
        + hidden(f"version_{index}", record.row_version)
        + f"<fieldset><legend>What have you done in Sleeper?</legend>{options}</fieldset></section>"
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
    if pending:
        summary = '<p class="notice">Decision saved. A new plan is queued or running. '
        summary += '<a href="/manage">Refresh to check the result</a>.</p>'
    elif plan:
        summary = (
            f"<section><h2>{escape(plan.title)}</h2><pre>{escape(plan.message)}</pre></section>"
        )
    else:
        summary = (
            '<p class="notice">A current plan is unavailable. Your decisions are saved.</p>'
            '<form method="post" action="/manage/replan">'
            + hidden("csrf", csrf)
            + hidden("request_id", uuid4().hex)
            + '<button type="submit">Retry plan</button></form>'
        )
    active = lockable_games(records, context, now)
    corrections = tuple(row for row in records if row.acknowledged_action)
    advice = None if pending else plan
    cards = (
        "".join(
            decision_card(row, context, i, now=now, plan=advice) for i, row in enumerate(active)
        )
        or "<section>No lockable games to review yet.</section>"
    )
    if corrections:
        cards += "<details><summary>Locked this week / correct a recorded action</summary>"
        cards += "<p>Correct a recorded action only if the saved record is wrong.</p>"
        cards += (
            "".join(
                decision_card(row, context, i, now=now, plan=advice)
                for i, row in enumerate(corrections, start=len(active))
            )
            + "</details>"
        )
    if active or corrections:
        cards = (
            '<form method="post" action="/manage/save">'
            + hidden("csrf", csrf)
            + hidden("batch_id", uuid4().hex)
            + hidden("count", len(active) + len(corrections))
            + cards
            + '<button type="submit">Save changes and update plan</button></form>'
        )
    visible = {row.key for row in (*active, *corrections)}
    past = "".join(
        f"<li>{escape(context.names.get(row.key.player_id, row.key.player_id))} · "
        f"{escape(_moment(row.scheduled_start, context))} · {row.stable_score:.2f} pts<br>"
        f"{recorded_state(row, now, context=context, records=records)}</li>"
        for row in records
        if row.key not in visible and row.stable_score is not None
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
        + cards
        + (f"<details><summary>Past games</summary><ul>{past}</ul></details>" if past else "")
        + "<details><summary>Correction history</summary><ul>"
        + ("".join(entries) or "<li>No manual corrections yet.</li>")
        + '</ul></details><form method="post" action="/manage/logout">'
        + hidden("csrf", csrf)
        + '<button type="submit">Sign out</button></form>',
    )
