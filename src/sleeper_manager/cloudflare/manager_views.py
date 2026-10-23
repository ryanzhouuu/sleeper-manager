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
from sleeper_manager.cloudflare.manager_icons import icon
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


def _moment(value: datetime, context: ManagerContext, *, compact: bool = False) -> str:
    return value.astimezone(ZoneInfo(context.timezone)).strftime(
        "%a %I:%M %p" if compact else "%a %b %d, %I:%M %p %Z"
    )


def _badge(state: str, source: str) -> str:
    label = state.split(" · ")[0].replace("Locked in Sleeper", "Locked")
    tone = {"Lock": "lock", "Locked": "lock", "Pass": "pass", "Passed": "pass", "Wait": "wait"}
    kind = tone.get(label, "alert")
    return (
        f'<span class="chip chip--{kind}" aria-label="{escape(source)}: {escape(state)}" '
        f'title="{escape(state)}">{icon(kind)}{escape(label)}</span>'
    )


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
        f'<label class="choice" title="{escape(LABELS[value])}">'
        f'<input type="radio" name="action_{index}" aria-label="{escape(LABELS[value])}" '
        f'value="{value}"{" checked" if value == action else ""}>'
        f'<span class="option">{icon(symbol)}{label}</span></label>'
        for value, symbol, label in (
            ("not_locked", "wait", "Wait"),
            ("locked", "lock", "Lock"),
            ("passed", "pass", "Pass"),
        )
    )
    score = "—" if record.stable_score is None else f"{record.stable_score:.2f}"
    slot = context.actual_slot(key.player_id)
    placement = slot[1] if slot else "Bench / reserve"
    deadline = escape(_moment(record.action_deadline, context))
    return (
        f'<section class="game-card" aria-label="{escape(name)}">'
        f'<div class="game-head"><div><h2>{escape(name)}</h2><p class="game-meta">'
        f"{escape(placement)} · {escape(_moment(record.scheduled_start, context, compact=True))}"
        f'</p></div><div class="game-score">{score}<small>PTS</small></div></div>'
        '<div class="game-state"><div class="state-group"><span class="muted">Plan</span>'
        + _badge(recommendation_for(record, plan, now), "Advisor")
        + '</div><div class="state-group"><span class="muted">Saved</span>'
        + _badge(recorded_state(record, now), "Recorded")
        + "</div></div>"
        + hidden(f"player_id_{index}", key.player_id)
        + hidden(f"game_id_{index}", key.game_id)
        + hidden(f"version_{index}", record.row_version)
        + f'<fieldset><legend class="sr-only">Your choice for {escape(name)}</legend>'
        f'<div class="choices">{options}</div></fieldset>'
        f'<p class="game-deadline" title="Lock window ends: {deadline}">'
        f'{icon("wait")}<span class="sr-only">Lock window ends </span>'
        f'<time datetime="{record.action_deadline.isoformat()}">'
        f"{escape(_moment(record.action_deadline, context, compact=True))}</time></p></section>"
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
    """Keep recorded decisions separate from advice and expose previous values for corrections."""
    if pending:
        summary = '<p class="notice">↻ Updating plan · <a href="/manage">Refresh</a></p>'
    elif plan:
        summary = (
            '<details class="plan-details"><summary>Team plan</summary>'
            f"<h2>{escape(plan.title)}</h2><pre>{escape(plan.message)}</pre></details>"
        )
    else:
        summary = (
            '<p class="notice">⚠ Plan unavailable</p>'
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
        or '<section class="game-card muted">No lockable games yet.</section>'
    )
    if corrections:
        cards += f"<details><summary>Saved decisions · {len(corrections)}</summary>"
        cards += '<p class="muted">Edit to correct a saved decision.</p>'
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
            + f'<h2 class="section-heading">To review <span class="count">{len(active)}</span></h2>'
            + cards
            + '<div class="save-bar"><small>Updates your plan</small>'
            '<button type="submit">Save &amp; replan ' + icon("arrow") + "</button></div></form>"
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
    timezone = escape(now.astimezone(ZoneInfo(context.timezone)).tzname() or "")
    return page(
        f"Your decisions · week {context.week}",
        '<header class="manager-head"><h1>Game decisions</h1>'
        f'<p class="muted">W{context.week} · {timezone}</p></header>'
        + summary
        + cards
        + (f"<details><summary>Past games</summary><ul>{past}</ul></details>" if past else "")
        + "<details><summary>History</summary><ul>"
        + ("".join(entries) or "<li>No manual corrections yet.</li>")
        + '</ul></details><details class="manager-help"><summary>How it works</summary>'
        f"<p>{icon('wait')} <strong>Wait</strong> · Undecided.</p>"
        f"<p>{icon('lock')} <strong>Lock</strong> · Already locked in Sleeper.</p>"
        f"<p>{icon('pass')} <strong>Pass</strong> · Choose not to lock. Substitution optional.</p>"
        "<p>Saving updates the plan, not your Sleeper lineup.</p></details>"
        '<footer class="manager-footer"><span class="muted">Sleeper · Lock-In</span>'
        '<form method="post" action="/manage/logout">'
        + hidden("csrf", csrf)
        + '<button type="submit">Sign out ↗</button></form></footer>',
        heading=False,
    )
