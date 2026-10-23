"""Expired work and racing computations must never revive advice predating saved decisions."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.cloudflare.home import _recommendation
from sleeper_manager.persistence.base import RecommendationRecord
from sleeper_manager.workflows.manager_decisions import ManagerAction
from tests.sleeper_manager.persistence.test_manager_store import NOW, row
from tests.sleeper_manager.persistence.test_manager_store import store as store


def plan(version=None, identity="plan"):  # type: ignore[no-untyped-def]
    trace = {"schema_version": 3, "status": "no_action", "score_actions": []}
    if version is not None:
        trace["manager_decision_version"] = version
    return RecommendationRecord(
        identity,
        identity,
        "league",
        1,
        "weekly-lineup",
        None,
        "weekly_lineup",
        "Old plan",
        "Already locked (confirmed)",
        NOW + timedelta(days=1),
        "v1",
        NOW,
        trace_json=json.dumps(trace),
    )


def correct(store, action=ManagerAction.NOT_LOCKED, identity="edit"):  # type: ignore[no-untyped-def]
    assert asyncio.run(
        store.save_decision(row(store), action, change_id=identity, now=NOW, actual_slot=(0, "PF"))
    )


def test_expired_failed_replan_does_not_restore_old_recommendation(store):  # type: ignore[no-untyped-def]
    asyncio.run(store.create_recommendation(plan()))
    correct(store)
    store._database.connection.execute("UPDATE scheduled_work SET status='canceled'")
    store._database.connection.execute("UPDATE recommendations SET status='expired'")
    store._database.connection.commit()
    result = asyncio.run(_recommendation(store, "league", 1, now=NOW + timedelta(hours=2)))
    assert result[2] == "Plan update unavailable"
    assert "Already locked" not in str(result)


def test_new_plan_must_include_latest_saved_decision_version(store):  # type: ignore[no-untyped-def]
    correct(store)
    version = asyncio.run(store.decision_state_version("league", 1))
    assert asyncio.run(store.create_recommendation(plan(version)))
    current = asyncio.run(
        store.latest_current_recommendation("league", 1, decision_type="weekly_lineup")
    )
    assert current is not None
    correct(store, ManagerAction.LOCKED, "newer")
    assert (
        asyncio.run(store.latest_current_recommendation("league", 1, decision_type="weekly_lineup"))
        is None
    )


def test_stale_plan_cannot_be_published_after_a_concurrent_save(store):  # type: ignore[no-untyped-def]
    captured = asyncio.run(store.decision_state_version("league", 1))
    correct(store)
    assert not asyncio.run(store.create_recommendation(plan(captured)))
    assert asyncio.run(store.get_recommendation("plan")) is None


def test_decision_versions_are_scoped_to_the_league_and_week(store):  # type: ignore[no-untyped-def]
    correct(store)
    assert asyncio.run(store.decision_state_version("league", 1)) == "1:0"
    assert asyncio.run(store.decision_state_version("other", 1)) == "0:0"
    assert asyncio.run(store.decision_state_version("league", 2)) == "0:0"


def test_expired_advice_is_not_returned_as_current_even_without_edits(store):  # type: ignore[no-untyped-def]
    asyncio.run(store.create_recommendation(replace(plan(), deadline=NOW)))
    result = asyncio.run(_recommendation(store, "league", 1, now=NOW))
    assert "Already locked" not in str(result)


def test_computation_is_discarded_if_saved_decisions_change_during_the_run(store, monkeypatch):  # type: ignore[no-untyped-def]
    from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
    from sleeper_manager.workflows.current_team_plan import build_current_team_plan
    from tests.sleeper_manager.workflows.test_daily_plan import NOW as INPUT_NOW
    from tests.sleeper_manager.workflows.test_daily_plan import _inputs, _projections

    versions = iter(("0:0", "1:0"))

    async def read(*args):  # type: ignore[no-untyped-def]
        return next(versions)

    monkeypatch.setattr(store, "decision_state_version", read)
    with pytest.raises(RuntimeError, match="changed during planning"):
        asyncio.run(
            build_current_team_plan(
                _inputs(projections=_projections(p1_expected=10, p2_expected=20)),
                store,
                INPUT_NOW,
                WeeklyPlanPolicyConfig(scenario_count=8),
                lambda: INPUT_NOW,
            )
        )


def test_corrected_lock_is_not_reintroduced_from_collected_repository_evidence(store):  # type: ignore[no-untyped-def]
    from sleeper_manager.domain.planning import AcknowledgedAction, AcknowledgedDecisionEvidence
    from sleeper_manager.workflows.recommendation_state import refreshed_team_state
    from tests.sleeper_manager.workflows.test_daily_plan import NOW as INPUT_NOW
    from tests.sleeper_manager.workflows.test_daily_plan import _inputs, _projections

    stale = AcknowledgedDecisionEvidence(
        "old",
        "p1",
        "done",
        AcknowledgedAction.LOCK,
        INPUT_NOW,
        "repository_acknowledgement_v1",
        0,
        "PG",
        30,
    )
    inputs = replace(
        _inputs(projections=_projections(p1_expected=10, p2_expected=20)), acknowledgements=(stale,)
    )
    state = asyncio.run(refreshed_team_state(inputs, store, INPUT_NOW))
    assert state.fixed_slots == ()
