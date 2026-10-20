"""Render live Lock-In results and canonical identities for durable notifications."""

import json
from hashlib import sha256

from sleeper_manager.domain.lock_in import LockInEvaluation, LockInEvaluationKind
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityRecord


def _evaluation_hash(evaluation: LockInEvaluation) -> str:
    """Hash canonical rounded material advice while excluding retrieval timestamps."""

    payload = {
        "kind": evaluation.kind.value,
        "score": _rounded(evaluation.observed_score),
        "alternative": _rounded(evaluation.alternative_expected_score),
        "expected_terminal_score": _rounded(
            evaluation.decision.expected_terminal_score if evaluation.decision else None
        ),
        "percentiles": [
            [rank, _rounded(value)] for rank, value in evaluation.alternative_percentiles
        ],
        "confidence": _rounded(evaluation.confidence),
        "reason_codes": evaluation.reason_codes,
        "trace": evaluation.trace,
        "slot_index": evaluation.decision.slot_index if evaluation.decision else None,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def _rounded(value: float | None) -> float | None:
    """Canonicalize material numeric evidence to two fantasy-score decimals."""

    return round(value, 2) if value is not None else None


def _evaluation_trace_json(
    evaluation: LockInEvaluation,
    opportunity: LockInOpportunityRecord,
) -> str:
    """Serialize the auditable live evaluation trace for persistence and acknowledgements."""

    payload: dict[str, object] = {
        "kind": evaluation.kind.value,
        "reason_codes": evaluation.reason_codes,
        "trace": dict(evaluation.trace),
        "observed_score": evaluation.observed_score,
        "alternative_expected_score": evaluation.alternative_expected_score,
        "expected_terminal_score": (
            evaluation.decision.expected_terminal_score if evaluation.decision else None
        ),
        "alternative_percentiles": evaluation.alternative_percentiles,
        "confidence": evaluation.confidence,
    }
    if evaluation.kind is LockInEvaluationKind.LOCK:
        payload["acknowledgement"] = {
            "schema_version": 1,
            "slot_index": evaluation.decision.slot_index if evaluation.decision else None,
            "slot_position": opportunity.slot_position,
            "accepted_fantasy_score": evaluation.observed_score,
        }
    elif evaluation.kind is LockInEvaluationKind.PASS:
        payload["acknowledgement"] = {"schema_version": 1}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _material_idempotency_key(
    opportunity: LockInOpportunityRecord,
    evaluation: LockInEvaluation,
    evaluation_hash: str,
) -> str:
    """Identify one material player-game recommendation across retries and restarts."""

    return ":".join(
        (
            opportunity.key.league_id,
            str(opportunity.key.fantasy_week),
            str(opportunity.key.roster_id),
            opportunity.key.player_id,
            opportunity.key.game_id,
            evaluation.manager_policy_version,
            str(opportunity.score_revision),
            evaluation.kind.value,
            evaluation.deadline.isoformat(),
            evaluation_hash,
        )
    )


def _evaluation_title(
    evaluation: LockInEvaluation,
    player_names: dict[str, str],
) -> str:
    """Render a compact actionable or warning title."""

    player = player_names.get(evaluation.player_id, evaluation.player_id)
    if evaluation.kind is LockInEvaluationKind.UNAVAILABLE:
        return f"Lock-In unavailable: {player}"
    return f"{evaluation.kind.value.title()} {player}?"


def _evaluation_message(evaluation: LockInEvaluation) -> str:
    """Separate the completed player's score from selected and alternative team totals."""

    if evaluation.kind is LockInEvaluationKind.UNAVAILABLE:
        return (
            "Safe Lock-In advice is unavailable: "
            + ", ".join(evaluation.reason_codes)
            + "\n\nExpected weekly total: unavailable."
        )
    assert evaluation.observed_score is not None
    assert evaluation.alternative_expected_score is not None
    assert evaluation.confidence is not None
    assert evaluation.decision is not None
    alternative = "pass" if evaluation.kind is LockInEvaluationKind.LOCK else "lock"
    return (
        f"Observed game score: {evaluation.observed_score:.2f} pts.\n\n"
        f"Expected weekly total: {evaluation.decision.expected_terminal_score:.1f} pts.\n"
        f"Alternative ({alternative}): {evaluation.alternative_expected_score:.1f} pts.\n\n"
        f"Confidence: {evaluation.confidence:.0%}. "
        "Complete the action in Sleeper, then acknowledge it."
    )
