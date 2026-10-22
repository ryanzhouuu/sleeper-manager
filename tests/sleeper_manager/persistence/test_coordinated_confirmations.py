"""Confirmed pre-lock realignment has identical exact-position semantics on SQLite and D1."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.persistence.base import (
    AcknowledgementAction,
    AcknowledgementOutcome,
    ActionTokenRecord,
    RecommendationRecord,
)
from sleeper_manager.persistence.tokens import hash_action_token
from tests.sleeper_manager.persistence.test_lock_in_opportunity_persistence import (
    NOW,
    _opportunity,
    _repository,
)


@pytest.mark.parametrize("backend", ("sqlite", "d1"))
@pytest.mark.parametrize("valid", (True, False))
def test_only_a_valid_confirmation_can_accept_the_exact_recommended_position(
    backend: str, valid: bool, tmp_path: Path
) -> None:
    async def exercise() -> None:
        repository = await _repository(backend, tmp_path)
        trace = json.dumps(
            {
                "acknowledgement": {
                    "schema_version": 1,
                    "slot_index": 0,
                    "slot_position": "PG",
                    "accepted_fantasy_score": 30,
                }
            }
        )
        recommendation = RecommendationRecord(
            "rec",
            "key",
            "league",
            1,
            "player",
            "game",
            "live_lock_in",
            "Lock player?",
            "Move to PG, then lock.",
            NOW + timedelta(hours=1),
            "policy",
            NOW,
            trace_json=trace,
        )
        await repository.create_recommendation(recommendation)
        row = replace(
            _opportunity(),
            status=LockInOpportunityStatus.ACTIONABLE,
            current_recommendation_id="rec",
            stable_score=30,
        )
        await repository.upsert_lock_in_opportunity(row)
        await repository.create_action_token(
            ActionTokenRecord(
                hash_action_token("synthetic"),
                "rec",
                AcknowledgementAction.LOCKED,
                NOW,
                NOW + timedelta(hours=1),
            )
        )
        result = await repository.consume_action_token(
            hash_action_token("synthetic"),
            AcknowledgementAction.LOCKED if valid else AcknowledgementAction.PASSED,
            NOW + timedelta(minutes=1),
        )
        stored = await repository.get_lock_in_opportunity(row.key)
        assert stored is not None
        if valid:
            assert result.outcome is AcknowledgementOutcome.APPLIED
            assert (stored.slot_index, stored.slot_position) == (0, "PG")
            assert stored.status is LockInOpportunityStatus.ACKNOWLEDGED_LOCKED
        else:
            assert result.outcome is AcknowledgementOutcome.CONFLICT
            assert stored == row

    asyncio.run(exercise())
