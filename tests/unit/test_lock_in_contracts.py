"""Contract coverage for production-neutral Lock-In decisions and traces."""

from datetime import UTC, datetime

import pytest

from sleeper_manager.domain.lock_in import (
    LockInContractError,
    LockInDecision,
    LockInDecisionKind,
    LockInDecisionTrace,
)

NOW = datetime(2026, 2, 2, 20, tzinfo=UTC)


def _decision(
    *,
    kind: LockInDecisionKind = LockInDecisionKind.LOCK,
    slot_index: int | None = 0,
) -> LockInDecision:
    """Build one valid deterministic decision for invariant tests."""

    return LockInDecision(
        decision_time=NOW,
        kind=kind,
        player_id="player-1",
        game_id="game-1",
        slot_index=slot_index,
        expected_terminal_score=100.0,
        counterfactual_value=5.0,
        information_version="inputs-v1",
        reason="LOCK retained the best terminal value.",
    )


def test_lock_and_pass_enforce_slot_semantics() -> None:
    """Prevent impossible action records from entering replay or live state."""

    assert _decision().slot_index == 0
    assert _decision(kind=LockInDecisionKind.PASS, slot_index=None).slot_index is None

    with pytest.raises(LockInContractError, match="Lock decisions require"):
        _decision(slot_index=None)
    with pytest.raises(LockInContractError, match="Pass decisions cannot"):
        _decision(kind=LockInDecisionKind.PASS, slot_index=0)


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"decision_time": datetime(2026, 2, 2, 20)}, "timezone-aware"),
        ({"player_id": "  "}, "player ID"),
        ({"game_id": ""}, "game ID"),
        ({"expected_terminal_score": float("nan")}, "values must be finite"),
        ({"counterfactual_value": float("inf")}, "values must be finite"),
        ({"information_version": ""}, "information version"),
        ({"reason": "  "}, "decision reason"),
    ),
)
def test_decision_rejects_incomplete_audit_evidence(
    changes: dict[str, object], message: str
) -> None:
    """Require deterministic identity and finite audit values on every decision."""

    values: dict[str, object] = {
        "decision_time": NOW,
        "kind": LockInDecisionKind.LOCK,
        "player_id": "player-1",
        "game_id": "game-1",
        "slot_index": 0,
        "expected_terminal_score": 100.0,
        "counterfactual_value": 5.0,
        "information_version": "inputs-v1",
        "reason": "LOCK retained the best terminal value.",
    }
    values.update(changes)

    with pytest.raises(LockInContractError, match=message):
        LockInDecision(**values)  # type: ignore[arg-type]


def test_trace_requires_stable_positive_ordering_identity() -> None:
    """Keep chronological reports reproducible across equivalent executions."""

    trace = LockInDecisionTrace(
        decision=_decision(),
        event_id="planning:2026-02-02T20:00:00+00:00",
        batch_id="batch:2026-02-02T19:00:00+00:00",
        candidate_id="player-1:game-1",
        evaluation_order=1,
    )

    assert trace.decision is not None
    with pytest.raises(LockInContractError, match="event ID"):
        LockInDecisionTrace(trace.decision, "", trace.batch_id, trace.candidate_id, 1)
    with pytest.raises(LockInContractError, match="order must be positive"):
        LockInDecisionTrace(
            trace.decision,
            trace.event_id,
            trace.batch_id,
            trace.candidate_id,
            0,
        )
