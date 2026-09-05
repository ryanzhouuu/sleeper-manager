"""Provenance must survive position selection and assembly coverage accounting."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from test_replay_inputs import _historical_join_inputs

from sleeper_manager.backtesting.replay.inputs import (
    assemble_historical_team_week_inputs,
    build_replay_input_manifest,
)
from sleeper_manager.domain.planning import PlanningQuality, PlanningReasonCode


@pytest.mark.parametrize("confidence", ["historical_proxy", "current_catalog", "unknown", ""])
def test_early_approximation_never_counts_as_exact_eligibility(confidence: str) -> None:
    inputs = _historical_join_inputs()
    evidence = replace(inputs.eligibility_evidence[0], confidence=confidence)
    result = assemble_historical_team_week_inputs(
        replace(inputs, eligibility_evidence=(evidence,))
    )[0]

    assert result.coverage.expected_player_games == 2
    assert result.coverage.exact_eligibility == 0
    assert result.coverage.best_known_eligibility == 2
    assert result.eligibility_quality is PlanningQuality.BEST_KNOWN_CONSTRAINTS_ORACLE
    assert not result.complete
    assert len(result.player_games) == 2


def test_exact_label_without_source_is_not_exact_evidence() -> None:
    inputs = _historical_join_inputs()
    evidence = replace(inputs.eligibility_evidence[0], source=" ")
    result = assemble_historical_team_week_inputs(
        replace(inputs, eligibility_evidence=(evidence,))
    )[0]

    assert result.coverage.exact_eligibility == 0
    assert not result.complete


def test_newer_proxy_replaces_older_exact_positions_without_inheriting_quality() -> None:
    inputs = _historical_join_inputs()
    older = inputs.eligibility_evidence[0]
    newer = replace(
        older,
        available_as_of=older.available_as_of + timedelta(days=1),
        eligible_positions=("F",),
        confidence="historical_proxy",
    )
    result = assemble_historical_team_week_inputs(
        replace(inputs, eligibility_evidence=(older, newer))
    )[0]

    assert all(game.eligible_positions == ("F",) for game in result.player_games)
    assert result.coverage.best_known_eligibility == 2
    assert not result.complete


@pytest.mark.parametrize("reverse", [False, True])
def test_equal_time_mixed_confidence_stays_best_known(reverse: bool) -> None:
    inputs = _historical_join_inputs()
    exact = inputs.eligibility_evidence[0]
    proxy = replace(exact, confidence="historical_proxy")
    evidence = (proxy, exact) if reverse else (exact, proxy)
    result = assemble_historical_team_week_inputs(replace(inputs, eligibility_evidence=evidence))[0]

    assert result.coverage.best_known_eligibility == 2
    assert not result.complete


@pytest.mark.parametrize("future", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_positions_at_selected_time_are_rejected(future: bool, reverse: bool) -> None:
    inputs = _historical_join_inputs()
    first = inputs.eligibility_evidence[0]
    if future:
        first = replace(first, available_as_of=datetime(2026, 2, 1, tzinfo=UTC))
    second = replace(first, eligible_positions=("F",))
    evidence = (second, first) if reverse else (first, second)
    result = assemble_historical_team_week_inputs(replace(inputs, eligibility_evidence=evidence))[0]

    assert result.eligibility_quality is PlanningQuality.PARTIAL
    assert not result.player_games
    assert not result.complete
    assert {issue.reason for issue in result.exclusions} == {
        PlanningReasonCode.AMBIGUOUS_ELIGIBILITY
    }


def test_future_exact_snapshot_is_only_a_best_known_fallback() -> None:
    inputs = _historical_join_inputs()
    future = replace(
        inputs.eligibility_evidence[0], available_as_of=datetime(2026, 2, 1, tzinfo=UTC)
    )
    result = assemble_historical_team_week_inputs(replace(inputs, eligibility_evidence=(future,)))[
        0
    ]

    assert result.coverage.best_known_eligibility == 2
    assert not result.complete


def test_provenance_policy_gets_a_distinct_manifest_identity() -> None:
    inputs = _historical_join_inputs()
    previous = replace(inputs, eligibility_policy_version="eligibility-v1")

    assert build_replay_input_manifest(inputs).manifest_id != (
        build_replay_input_manifest(previous).manifest_id
    )
