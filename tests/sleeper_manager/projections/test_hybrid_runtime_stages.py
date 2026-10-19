"""Runtime profiling preserves projections and exposes the last phase before failure."""

import asyncio
import json

import pytest

from sleeper_manager.cloudflare.stage_logging import timed_stage
from sleeper_manager.projections.hybrid_runtime_provider import HybridRuntimeProjectionProvider
from tests.sleeper_manager.projections.hybrid_support import CUTOFF
from tests.sleeper_manager.projections.test_hybrid_model import TARGET
from tests.sleeper_manager.projections.test_hybrid_runtime_index import POLICY
from tests.sleeper_manager.projections.test_hybrid_runtime_provider import HistoryStore


def test_profiling_preserves_results_and_omits_player_payloads(capsys) -> None:  # type: ignore[no-untyped-def]
    """Observe the real batch without adding player identifiers or forecast values to logs."""
    plain = HybridRuntimeProjectionProvider(HistoryStore(), "version")
    observed = HybridRuntimeProjectionProvider(HistoryStore(), "version", stage=timed_stage)

    async def exercise(provider):  # type: ignore[no-untyped-def]
        return await provider.project_batch((TARGET,), scoring_policy=POLICY, decision_time=CUTOFF)

    assert asyncio.run(exercise(observed)) == asyncio.run(exercise(plain))
    output = capsys.readouterr().out
    events = [json.loads(line) for line in output.splitlines()]
    assert events[0]["stage"] == "hybrid_history_load"
    assert events[-1]["stage"] == "hybrid_player_project"
    assert [row["outcome"] for row in events] == ["started", "completed"] * 5
    assert all(set(row) <= {"event", "stage", "outcome", "wall_ms"} for row in events)
    assert TARGET.sleeper_player_id not in output
    assert TARGET.game_id not in output


def test_partition_termination_keeps_last_phase_and_reraises_without_payload(capsys) -> None:  # type: ignore[no-untyped-def]
    """An unhandled termination must not become a successful or fallback projection."""

    class Termination(BaseException):
        pass

    class BrokenHistory(HistoryStore):
        async def load_player_games(self, *args):  # type: ignore[no-untyped-def]
            raise Termination("private historical payload")

    provider = HybridRuntimeProjectionProvider(BrokenHistory(), "version", stage=timed_stage)
    with pytest.raises(Termination):
        asyncio.run(provider.project_batch((TARGET,), scoring_policy=POLICY, decision_time=CUTOFF))
    output = capsys.readouterr().out
    events = [json.loads(line) for line in output.splitlines()]
    assert events[-1]["stage"] == "hybrid_partitions_load"
    assert events[-1]["outcome"] == "failed"
    assert events[-1]["error_type"] == "Termination"
    assert "private historical payload" not in output
    assert provider.results == ()
