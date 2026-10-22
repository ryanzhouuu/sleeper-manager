"""Cards expose verified lock windows and keep advice separate from recorded choices."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.cloudflare.manager_context import ManagerContext
from sleeper_manager.cloudflare.manager_games import lockable_games, recommendation_for
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
    assert "Advisor: <strong>Pass</strong>" in html
    assert "Recorded: <strong>Wait</strong>" in html
    assert 'value="not_locked" checked' in html
    assert 'value="locked"' in html
    assert html.count("Save changes and update plan") == 1
    assert html.count('action="/manage/save"') == 1
    assert 'name="batch_id"' in html


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
    assert "Locked this week" in html
    assert "Locked in Sleeper" in html
    assert "Correct a recorded action" in html
