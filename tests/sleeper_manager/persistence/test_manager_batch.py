"""One submitted set of decisions commits atomically and schedules exactly one replan."""

import asyncio
from dataclasses import replace

import pytest

from sleeper_manager.workflows.manager_decisions import ManagerAction, ManagerEdit
from tests.sleeper_manager.persistence.test_manager_store import KEY, NOW, row
from tests.sleeper_manager.persistence.test_manager_store import store as store


def edits(store):  # type: ignore[no-untyped-def]
    first = row(store)
    second = replace(
        first,
        key=replace(KEY, player_id="other", game_id="other-game"),
        slot_index=1,
        current_recommendation_id=None,
        acknowledged_action=None,
    )
    asyncio.run(store.upsert_lock_in_opportunity(second))
    return (
        ManagerEdit(first, ManagerAction.NOT_LOCKED, (0, "PF")),
        ManagerEdit(second, ManagerAction.LOCKED, (1, "PF")),
    )


def submit(store, changes, batch_id="batch-1"):  # type: ignore[no-untyped-def]
    return asyncio.run(store.save_decisions(changes, batch_id=batch_id, now=NOW))


def test_batch_saves_every_action_and_queues_once(store):  # type: ignore[no-untyped-def]
    changes = edits(store)
    assert submit(store, changes)
    assert row(store).acknowledged_action is None
    other = asyncio.run(store.get_lock_in_opportunity(changes[1].current.key))
    assert other.acknowledged_action == "locked"
    assert len(asyncio.run(store.decision_history("league", 1, 1))) == 2
    work = asyncio.run(store.list_scheduled_work())
    assert len(work) == 1 and work[0].dedupe_key == "manual:manager:batch-1"


def test_one_stale_row_rejects_entire_batch(store):  # type: ignore[no-untyped-def]
    changes = edits(store)
    changes = (changes[0], replace(changes[1], current=replace(changes[1].current, row_version=9)))
    assert not submit(store, changes)
    assert row(store).acknowledged_action == "locked"
    assert asyncio.run(store.decision_history("league", 1, 1)) == []
    assert asyncio.run(store.list_scheduled_work()) == ()


def test_replay_does_not_overwrite_subsequent_actions_or_enqueue_again(store):  # type: ignore[no-untyped-def]
    changes = edits(store)
    assert submit(store, changes)
    assert submit(store, (ManagerEdit(row(store), ManagerAction.PASSED, (0, "PF")),), "later")
    assert submit(store, changes)
    assert row(store).acknowledged_action == "passed"
    assert len(asyncio.run(store.list_scheduled_work())) == 2


def test_batch_rejects_duplicate_slots_without_partial_writes(store):  # type: ignore[no-untyped-def]
    changes = edits(store)
    changes = (
        replace(changes[0], action=ManagerAction.LOCKED),
        replace(changes[1], actual_slot=(0, "PF")),
    )
    assert not submit(store, changes)
    assert asyncio.run(store.decision_history("league", 1, 1)) == []


def test_batch_can_correct_false_lock_before_recording_the_real_slot_owner(store):  # type: ignore[no-untyped-def]
    changes = edits(store)
    assert submit(store, (changes[0], replace(changes[1], actual_slot=(0, "PF"))))
    assert row(store).acknowledged_action is None


def test_write_failure_rolls_back_all_actions_and_replan(store):  # type: ignore[no-untyped-def]
    changes = edits(store)
    store._database.connection.executescript(
        "CREATE TRIGGER fail_batch BEFORE UPDATE ON recommendations "
        "BEGIN SELECT RAISE(ABORT,'injected write failure'); END;"
    )
    with pytest.raises(Exception, match="injected write failure"):
        submit(store, changes)
    assert row(store).acknowledged_action == "locked"
    assert asyncio.run(store.decision_history("league", 1, 1)) == []
    assert asyncio.run(store.list_scheduled_work()) == ()


def test_reusing_batch_identity_for_other_actions_is_rejected(store):  # type: ignore[no-untyped-def]
    changes = edits(store)
    assert submit(store, changes)
    assert not submit(store, (changes[0], replace(changes[1], action=ManagerAction.PASSED)))
