"""Corrections retain historical confirmations but remove them from effective planning state."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.persistence.base import (
    AcknowledgementAction,
    RecommendationRecord,
    RecommendationStatus,
)
from sleeper_manager.persistence.d1 import D1_SCHEMA
from sleeper_manager.persistence.lock_in_opportunities import (
    LockInOpportunityKey,
    LockInOpportunityRecord,
)
from sleeper_manager.persistence.manager_store import D1ManagerStore
from sleeper_manager.workflows.lock_in_evidence import evidence_from_lock_in_opportunity
from sleeper_manager.workflows.manager_decisions import ManagerAction, reported_decision
from tests.sleeper_manager.persistence.test_d1 import FakeD1

NOW = datetime(2026, 10, 22, 21, tzinfo=UTC)
KEY = LockInOpportunityKey("league", 1, 1, "player", "game")


@pytest.fixture
def store():  # type: ignore[no-untyped-def]
    db = FakeD1()
    asyncio.run(db.exec(D1_SCHEMA))
    asyncio.run(
        db.exec(Path("infra/cloudflare/migrations/0006_manager_decision_changes.sql").read_text())
    )
    repo = D1ManagerStore(db)
    asyncio.run(
        repo.create_recommendation(
            RecommendationRecord(
                recommendation_id="rec",
                idempotency_key="rec",
                league_id="league",
                fantasy_week=1,
                player_id="player",
                game_id="game",
                decision_type="live_lock_in",
                title="Pass player?",
                message="Observed game",
                deadline=NOW + timedelta(hours=1),
                policy_version="v1",
                created_at=NOW - timedelta(hours=1),
                status=RecommendationStatus.ACKNOWLEDGED,
                acknowledged_action=AcknowledgementAction.LOCKED,
                acknowledged_at=NOW - timedelta(hours=1),
            )
        )
    )
    asyncio.run(
        repo.upsert_lock_in_opportunity(
            LockInOpportunityRecord(
                key=KEY,
                provider_player_id="provider",
                scheduled_start=NOW - timedelta(hours=4),
                action_deadline=NOW + timedelta(hours=2),
                fantasy_week_end=NOW + timedelta(days=3),
                status=LockInOpportunityStatus.ACKNOWLEDGED_LOCKED,
                next_check_at=NOW,
                created_at=NOW,
                updated_at=NOW,
                slot_index=0,
                slot_position="PF",
                eligible_positions=("PF",),
                rostered_at_tipoff=True,
                stable_score=32.8,
                current_recommendation_id="rec",
                acknowledged_action="locked",
                acknowledged_at=NOW - timedelta(hours=1),
            )
        )
    )
    return repo


def row(store):  # type: ignore[no-untyped-def]
    return asyncio.run(store.get_lock_in_opportunity(KEY))


def save(store, action=ManagerAction.NOT_LOCKED, change_id="change-1", current=None):  # type: ignore[no-untyped-def]
    return asyncio.run(
        store.save_decision(
            current or row(store),
            action,
            change_id=change_id,
            now=NOW,
            actual_slot=(0, "PF"),
        )
    )


def test_correction_removes_false_lock_retains_score_and_history(store):  # type: ignore[no-untyped-def]
    original = row(store)
    assert save(store)
    current = row(store)
    assert current.status is LockInOpportunityStatus.FINALIZING
    assert current.acknowledged_action is None
    assert current.stable_score == 32.8
    assert evidence_from_lock_in_opportunity(current) is None
    assert asyncio.run(store.load_acknowledged_lock_in_opportunities("league", 1)) == ()
    history = asyncio.run(store.decision_history("league", 1, 1))
    previous = json.loads(history[0]["previous_state_json"])
    assert previous["acknowledged_action"] == "locked"
    assert current.row_version == original.row_version + 1
    assert asyncio.run(store.get_recommendation("rec")).status is RecommendationStatus.SUPERSEDED
    work = asyncio.run(store.list_scheduled_work())
    assert len(work) == 1 and work[0].dedupe_key == "manual:manager:change-1"


def test_same_request_replays_without_overwriting_later_decision(store):  # type: ignore[no-untyped-def]
    original = row(store)
    assert save(store, current=original)
    assert save(store, ManagerAction.PASSED, change_id="change-2")
    assert save(store, current=original)
    assert row(store).status is LockInOpportunityStatus.ACKNOWLEDGED_PASSED
    assert len(asyncio.run(store.decision_history("league", 1, 1))) == 2
    assert len(asyncio.run(store.list_scheduled_work())) == 2


def test_stale_edit_writes_nothing(store):  # type: ignore[no-untyped-def]
    stale = replace(row(store), row_version=10)
    assert not save(store, current=stale)
    assert row(store).acknowledged_action == "locked"
    assert asyncio.run(store.decision_history("league", 1, 1)) == []
    assert asyncio.run(store.list_scheduled_work()) == ()


@pytest.mark.parametrize(
    "other_key",
    [
        LockInOpportunityKey("league", 1, 1, "other", "other-game"),
        LockInOpportunityKey("league", 1, 1, "player", "other-game"),
    ],
)
def test_cannot_report_two_locks_for_same_slot_or_player(store, other_key):  # type: ignore[no-untyped-def]
    other = replace(row(store), key=other_key, current_recommendation_id=None)
    asyncio.run(store.upsert_lock_in_opportunity(other))
    assert not save(store, ManagerAction.LOCKED)
    assert asyncio.run(store.decision_history("league", 1, 1)) == []


def test_rejected_transaction_rolls_back_audit_decision_and_replan(store):  # type: ignore[no-untyped-def]
    store._database.connection.executescript(
        "CREATE TRIGGER fail_update BEFORE UPDATE ON recommendations "
        "BEGIN SELECT RAISE(ABORT,'injected write failure'); END;"
    )
    with pytest.raises(Exception, match="injected write failure"):
        save(store)
    assert row(store).acknowledged_action == "locked"
    assert asyncio.run(store.decision_history("league", 1, 1)) == []
    assert asyncio.run(store.list_scheduled_work()) == ()


@pytest.mark.parametrize(
    "changes,slot",
    [
        ({"stable_score": None}, (0, "PF")),
        ({"scheduled_start": NOW + timedelta(hours=1)}, (0, "PF")),
        ({"rostered_at_tipoff": False}, (0, "PF")),
        ({"action_deadline": NOW}, (0, "PF")),
        ({}, None),
        ({}, (0, "PG")),
    ],
)
def test_reports_require_real_eligible_score_and_live_slot(store, changes, slot):  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        reported_decision(
            replace(row(store), **changes), ManagerAction.LOCKED, NOW, actual_slot=slot
        )


def test_not_locked_does_not_revive_forfeited_or_expired_game(store):  # type: ignore[no-untyped-def]
    current = row(store)
    for change, expected in [
        ({"status": LockInOpportunityStatus.INELIGIBLE}, LockInOpportunityStatus.INELIGIBLE),
        ({"action_deadline": NOW}, LockInOpportunityStatus.EXPIRED),
    ]:
        corrected = reported_decision(
            replace(current, **change), ManagerAction.NOT_LOCKED, NOW, actual_slot=None
        )
        assert corrected.status is expected
        assert corrected.rostered_at_tipoff is current.rostered_at_tipoff


def test_superseded_legacy_ack_is_not_reloaded_as_a_false_lock(store):  # type: ignore[no-untyped-def]
    connection = store._database.connection
    connection.execute(
        "UPDATE recommendations SET decision_type='lock_in' WHERE recommendation_id='rec'"
    )
    connection.execute(
        "INSERT INTO action_tokens VALUES ('old-token','rec','locked',?,?,?)",
        (NOW.isoformat(), (NOW + timedelta(hours=1)).isoformat(), NOW.isoformat()),
    )
    connection.execute(
        "INSERT INTO acknowledgements VALUES ('original','rec','locked',?,'old-token')",
        ((NOW - timedelta(hours=1)).isoformat(),),
    )
    connection.commit()
    connection.execute(
        "INSERT INTO lock_acknowledgements VALUES ('rec','player',?)", (NOW.isoformat(),)
    )
    connection.commit()
    assert save(store)
    assert not asyncio.run(store.is_locked("rec"))
    assert asyncio.run(store.load_acknowledged_decisions("league", 1, as_of=NOW)) == ()
    assert connection.execute("SELECT COUNT(*) FROM acknowledgements").fetchone()[0] == 1


def test_replayed_ack_does_not_reset_a_completed_plan(store):  # type: ignore[no-untyped-def]
    asyncio.run(store.ensure_ack_replan("rec", NOW))
    store._database.connection.execute("UPDATE scheduled_work SET status='completed'")
    store._database.connection.commit()
    asyncio.run(store.ensure_ack_replan("rec", NOW + timedelta(minutes=1)))
    assert asyncio.run(store.list_scheduled_work())[0].status.value == "completed"


def test_concurrent_duplicate_id_cannot_apply_to_a_different_game(store, monkeypatch):  # type: ignore[no-untyped-def]
    original = row(store)
    assert save(store)
    other = replace(
        original,
        key=replace(KEY, player_id="other", game_id="other-game"),
        current_recommendation_id=None,
    )
    asyncio.run(store.upsert_lock_in_opportunity(other))
    first = store._first

    async def hide_existing(query, *params):  # type: ignore[no-untyped-def]
        if query.startswith("SELECT action,league_id"):
            return None
        return await first(query, *params)

    monkeypatch.setattr(store, "_first", hide_existing)
    assert not asyncio.run(
        store.save_decision(
            other, ManagerAction.NOT_LOCKED, change_id="change-1", now=NOW, actual_slot=None
        )
    )
    assert asyncio.run(store.get_lock_in_opportunity(other.key)).acknowledged_action == "locked"
    assert len(asyncio.run(store.decision_history("league", 1, 1))) == 1
