"""Lock/Pass messages distinguish completed player scores from weekly team estimates."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.domain.lock_in import (
    LockInDecision,
    LockInDecisionKind,
    LockInEvaluation,
    LockInEvaluationKind,
)
from sleeper_manager.workflows.lock_in_rendering import _evaluation_hash, _evaluation_message

NOW = datetime(2026, 1, 5, 12, tzinfo=UTC)


def _evaluation(kind: LockInEvaluationKind = LockInEvaluationKind.LOCK) -> LockInEvaluation:
    """Keep the player's observed score distinct from both team-level alternatives."""
    return LockInEvaluation(
        decision_time=NOW,
        kind=kind,
        player_id="p1",
        game_id="g1",
        deadline=NOW + timedelta(hours=1),
        information_version="inputs-v1",
        manager_policy_version="policy-v1",
        reason_codes=("confidence_met",),
        trace=(("projection_model_version", "model-v1"),),
        observed_score=35.0,
        alternative_expected_score=337.8,
        confidence=0.85,
        decision=LockInDecision(
            decision_time=NOW,
            kind=LockInDecisionKind(kind.value),
            player_id="p1",
            game_id="g1",
            slot_index=0 if kind is LockInEvaluationKind.LOCK else None,
            expected_terminal_score=342.6,
            counterfactual_value=4.8,
            information_version="inputs-v1",
            reason="Selected the higher team total.",
        ),
    )


@pytest.mark.parametrize(
    ("kind", "alternative"),
    ((LockInEvaluationKind.LOCK, "pass"), (LockInEvaluationKind.PASS, "lock")),
)
def test_actionable_messages_show_selected_and_alternative_team_totals(
    kind: LockInEvaluationKind, alternative: str
) -> None:
    message = _evaluation_message(_evaluation(kind))

    assert "Expected weekly total: 342.6 pts." in message
    assert f"Alternative ({alternative}): 337.8 pts." in message
    assert "Observed game score: 35.00 pts." in message
    assert "Confidence: 85%." in message
    assert "Complete the action in Sleeper, then acknowledge it." in message


def test_unavailable_advice_does_not_invent_a_weekly_estimate() -> None:
    evaluation = replace(
        _evaluation(),
        kind=LockInEvaluationKind.UNAVAILABLE,
        decision=None,
        reason_codes=("planning_state_blocked",),
    )

    message = _evaluation_message(evaluation)

    assert "Safe Lock-In advice is unavailable: planning_state_blocked" in message
    assert "Expected weekly total: unavailable." in message
    assert "337.8" not in message
    assert "342.6" not in message


def test_selected_weekly_estimate_changes_are_material() -> None:
    evaluation = _evaluation()
    assert evaluation.decision is not None
    changed = replace(
        evaluation, decision=replace(evaluation.decision, expected_terminal_score=350.0)
    )

    assert _evaluation_hash(changed) != _evaluation_hash(evaluation)
