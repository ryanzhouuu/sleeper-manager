"""Acknowledgement source and repository round-trips into live planning."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from sleeper_manager.domain.planning import (
    AcknowledgedAction,
    AcknowledgedDecisionEvidence,
    PlanningReasonCode,
)
from sleeper_manager.persistence.acknowledgements import (
    ACKNOWLEDGEMENT_PROVENANCE,
    AcknowledgementQueryError,
)
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import (
    AcknowledgementAction,
)
from sleeper_manager.workflows.planning_inputs import (
    build_live_team_week_state,
)
from tests.sleeper_manager.workflows.planning_collection_support import (
    NOW,
    _collect,
    _lock_in_recommendation,
    _nba,
    _RecordingProjections,
    _seed_acknowledgement,
    _StaticAcknowledgements,
    asyncio_run,
)


def test_acknowledgement_source_feeds_the_bundle() -> None:
    passed = AcknowledgedDecisionEvidence(
        decision_id="rec-pass-9",
        player_id="p1",
        game_id="g1",
        action=AcknowledgedAction.PASS,
        decided_at=NOW - timedelta(hours=2),
        provenance="repository-query",
    )
    source = _StaticAcknowledgements((passed,))

    async def run() -> None:
        return await _collect(_nba(), _RecordingProjections(), acknowledgement_source=source)

    evidence = asyncio_run(run())
    assert source.requested == ("league-1", 1)
    assert len(evidence.inputs.acknowledgements) == 1
    state = build_live_team_week_state(evidence.inputs, decision_time=evidence.decision_time)
    assert ("p1", "g1") not in {
        (item.sleeper_player_id, item.game_id) for item in state.unpassed_opportunities
    }


def test_async_sqlite_pass_survives_reopen_into_live_state(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    writer = AsyncSQLiteStateRepository(path)
    asyncio_run(writer.initialize())
    asyncio_run(
        _seed_acknowledgement(
            writer,
            _lock_in_recommendation(
                trace_json='{"acknowledgement":{"schema_version":1}}',
            ),
            AcknowledgementAction.PASSED,
            token="pass-live",
            at=NOW - timedelta(hours=2),
        )
    )
    source = AsyncSQLiteStateRepository(path)

    async def run() -> object:
        return await _collect(_nba(), _RecordingProjections(), acknowledgement_source=source)

    evidence = asyncio_run(run())
    state = build_live_team_week_state(evidence.inputs, decision_time=evidence.decision_time)
    remaining = {(item.sleeper_player_id, item.game_id) for item in state.unpassed_opportunities}
    assert ("p1", "g1") not in remaining
    assert ("p1", "g2") in remaining
    assert state.passed_opportunities[0].decision_time == NOW - timedelta(hours=2)
    assert not state.is_blocked


def test_async_sqlite_lock_round_trips_into_fixed_slot(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    writer = AsyncSQLiteStateRepository(path)
    asyncio_run(writer.initialize())
    asyncio_run(
        _seed_acknowledgement(
            writer,
            _lock_in_recommendation(),
            AcknowledgementAction.LOCKED,
            token="lock-live",
            at=NOW - timedelta(hours=2),
        )
    )
    source = AsyncSQLiteStateRepository(path)

    async def run() -> object:
        return await _collect(_nba(), _RecordingProjections(), acknowledgement_source=source)

    evidence = asyncio_run(run())
    state = build_live_team_week_state(evidence.inputs, decision_time=evidence.decision_time)
    assert len(state.fixed_slots) == 1
    fixed = state.fixed_slots[0]
    assert fixed.slot_index == 0
    assert fixed.slot_position == "PG"
    assert fixed.accepted_fantasy_score == 24
    assert fixed.decision_time == NOW - timedelta(hours=2)
    assert fixed.provenance == ACKNOWLEDGEMENT_PROVENANCE
    assert not state.is_blocked


def test_malformed_repository_trace_blocks_without_becoming_a_constraint(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    writer = AsyncSQLiteStateRepository(path)
    asyncio_run(writer.initialize())
    asyncio_run(
        _seed_acknowledgement(
            writer,
            _lock_in_recommendation(trace_json="{"),
            AcknowledgementAction.LOCKED,
            token="bad-live",
            at=NOW - timedelta(hours=2),
        )
    )
    source = AsyncSQLiteStateRepository(path)

    async def run() -> object:
        return await _collect(_nba(), _RecordingProjections(), acknowledgement_source=source)

    evidence = asyncio_run(run())
    assert len(evidence.inputs.acknowledgements) == 1
    assert evidence.inputs.acknowledgements[0].reconciled is False
    state = build_live_team_week_state(evidence.inputs, decision_time=evidence.decision_time)
    assert PlanningReasonCode.ACKNOWLEDGEMENT_CONFLICT in state.blocking_reasons
    assert state.fixed_slots == ()
    assert state.passed_opportunities == ()


def test_repository_acknowledgement_errors_propagate() -> None:
    class _BrokenSource:
        async def load_acknowledged_decisions(
            self, league_id: str, fantasy_week: int, *, as_of: datetime
        ) -> tuple[AcknowledgedDecisionEvidence, ...]:
            raise AcknowledgementQueryError("unexpected D1 result envelope")

    async def run() -> object:
        return await _collect(
            _nba(),
            _RecordingProjections(),
            acknowledgement_source=_BrokenSource(),
        )

    with pytest.raises(AcknowledgementQueryError, match="envelope"):
        asyncio_run(run())
