"""Cards expose verified lock windows and keep advice separate from recorded choices."""

import json
from dataclasses import replace
from datetime import timedelta
from html.parser import HTMLParser

import pytest

from sleeper_manager.cloudflare.home_document import HomeIdentity
from sleeper_manager.cloudflare.manager_context import ManagerContext
from sleeper_manager.cloudflare.manager_games import (
    lockable_games,
    recommendation_for,
    recorded_state,
)
from sleeper_manager.cloudflare.manager_views import editor_page
from sleeper_manager.domain.lock_in import LockInOpportunityStatus as Status
from sleeper_manager.persistence.base import RecommendationRecord
from tests.sleeper_manager.persistence.test_manager_store import KEY, NOW, row
from tests.sleeper_manager.persistence.test_manager_store import store as store

CONTEXT = ManagerContext("league", 1, 1, ("PF",), ("player",), {"player": "Player"}, "UTC")


def game(store):  # type: ignore[no-untyped-def]
    return replace(row(store), status=Status.ACTIONABLE, acknowledged_action=None)


@pytest.mark.parametrize(
    "changes",
    [
        {"stable_score": None},
        {"rostered_at_tipoff": None},
        {"rostered_at_tipoff": False},
        {"action_deadline": NOW},
        {"status": Status.INELIGIBLE},
        {"status": Status.RECONCILIATION_REQUIRED},
        {"scheduled_start": NOW + timedelta(hours=1)},
    ],
)
def test_active_cards_require_final_score_and_current_eligibility(store, changes):  # type: ignore[no-untyped-def]
    assert lockable_games((replace(game(store), **changes),), CONTEXT, NOW) == ()


def test_only_latest_eligible_game_per_player_is_active(store):  # type: ignore[no-untyped-def]
    latest = game(store)
    older = replace(
        latest, key=replace(KEY, game_id="older"), scheduled_start=NOW - timedelta(days=1)
    )
    assert lockable_games((older, latest), CONTEXT, NOW) == (latest,)
    bench = replace(CONTEXT, starters=("other",))
    assert lockable_games((latest,), bench, NOW) == ()


def plan(**overrides):  # type: ignore[no-untyped-def]
    return RecommendationRecord(
        "plan",
        "plan",
        "league",
        1,
        "weekly-lineup",
        None,
        "weekly_lineup",
        "Team plan",
        "Current advice",
        NOW + timedelta(hours=1),
        "v1",
        NOW,
        trace_json=json.dumps(
            {
                "schema_version": 3,
                "status": "action_required",
                "score_actions": [{"player_id": "player", "game_id": "game", "kind": "pass"}],
                **overrides,
            }
        ),
    )


def test_advice_pass_does_not_change_default_wait_or_lock_selection(store):  # type: ignore[no-untyped-def]
    current = game(store)
    html = editor_page(CONTEXT, (current,), [], "csrf", now=NOW, pending=False, plan=plan()).body
    assert 'aria-label="Advisor: Pass"' in html
    assert 'aria-label="Recorded: Wait"' in html
    assert 'value="not_locked" checked' in html
    assert 'value="locked"' in html
    assert html.count("Save &amp; replan") == 1
    assert html.count('action="/manage/save"') == 1
    assert 'name="batch_id"' in html
    assert "Wait</strong> · Undecided" in html
    assert "Already locked in Sleeper" in html
    assert "Choose not to lock. Substitution optional" in html
    assert "What have you done in Sleeper?" not in html


def test_wait_requires_a_current_valid_plan_and_missing_advice_is_unavailable(store):  # type: ignore[no-untyped-def]
    current = game(store)
    assert recommendation_for(current, None, NOW) == "Unavailable"
    assert recommendation_for(current, plan(status="blocked"), NOW) == "Unavailable"
    assert recommendation_for(current, plan(score_actions=[]), NOW) == "Wait"
    assert recommendation_for(current, replace(plan(), deadline=NOW), NOW) == "Unavailable"


def test_expired_games_move_to_history_without_claiming_a_manual_pass(store):  # type: ignore[no-untyped-def]
    current = replace(game(store), action_deadline=NOW)
    html = editor_page(CONTEXT, (current,), [], "csrf", now=NOW, pending=False, plan=None).body
    assert "Passed · lock window closed" in html
    assert 'name="action_0"' not in html
    assert "No lockable games" in html


def test_confirmed_locks_remain_visible_with_an_explicit_correction_control(store):  # type: ignore[no-untyped-def]
    html = editor_page(CONTEXT, (row(store),), [], "csrf", now=NOW, pending=False, plan=plan()).body
    assert "Saved decisions · 1" in html
    assert "Locked in Sleeper" in html
    assert "Edit to correct a saved decision" in html


def test_live_bench_or_next_start_cannot_leave_an_ineligible_game_waiting_for_a_poll(store):  # type: ignore[no-untyped-def]
    current = game(store)
    bench = replace(CONTEXT, starters=("other",))
    assert recorded_state(current, NOW, context=bench) == "Passed · eligibility lost"
    newer = replace(
        current, key=replace(KEY, game_id="next"), scheduled_start=NOW - timedelta(hours=1)
    )
    assert recorded_state(current, NOW, records=(newer,)) == "Passed · next game started"
    assert current.acknowledged_action is None


class Markup(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))


def test_compact_controls_keep_native_labels_and_details_closed(store):  # type: ignore[no-untyped-def]
    html = editor_page(
        CONTEXT, (game(store),), [], "csrf", now=NOW, pending=False, plan=plan()
    ).body
    markup = Markup()
    markup.feed(html)
    radios = [
        attrs for tag, attrs in markup.elements if tag == "input" and attrs.get("type") == "radio"
    ]
    assert [attrs["aria-label"] for attrs in radios] == ["Wait", "Locked in Sleeper", "Passed"]
    assert [attrs["value"] for attrs in radios] == ["not_locked", "locked", "passed"]
    assert sum(tag == "h1" for tag, _ in markup.elements) == 1
    assert all("open" not in attrs for tag, attrs in markup.elements if tag == "details")
    assert all(attrs.get("aria-hidden") == "true" for tag, attrs in markup.elements if tag == "svg")
    assert "Current advice" in html
    assert 'aria-label="Advisor: Pass"' in html
    assert 'aria-label="Recorded: Wait"' in html


def test_compact_cards_escape_names_in_headings_and_accessible_labels(store):  # type: ignore[no-untyped-def]
    context = replace(CONTEXT, names={"player": '<img src=x onerror="alert(1)">'})
    html = editor_page(context, (game(store),), [], "csrf", now=NOW, pending=False, plan=None).body
    assert "<img" not in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html
    assert 'aria-label="Advisor: Unavailable"' in html
    assert 'value="not_locked" checked' in html


@pytest.mark.parametrize("espn_id", ("55", None))
def test_cards_reuse_home_image_urls_with_optional_portraits(store, espn_id):  # type: ignore[no-untyped-def]
    context = replace(
        CONTEXT, identities={"player": HomeIdentity("player", "Player", "NOP", espn_id)}
    )
    result = editor_page(context, (game(store),), [], "csrf", now=NOW, pending=False, plan=None)
    markup = Markup()
    markup.feed(result.body)
    images = [attrs for tag, attrs in markup.elements if tag == "img"]
    assert len(images) == (2 if espn_id else 1)
    assert images[-1]["src"] == "https://a.espncdn.com/i/teamlogos/nba/500/no.png"
    assert images[-1]["alt"] == "NOP"
    if espn_id:
        assert images[0]["src"] == "https://a.espncdn.com/i/headshots/nba/players/full/55.png"
        assert images[0]["alt"] == ""
    assert all(image["loading"] == "lazy" for image in images)
    assert all(image["referrerpolicy"] == "no-referrer" for image in images)
    assert "img-src https://a.espncdn.com;" in result.headers["content-security-policy"]
    assert "default-src 'none';" in result.headers["content-security-policy"]
