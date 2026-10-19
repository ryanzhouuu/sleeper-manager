"""Prepared control batches retain independent projection outputs, errors, and cutoff gaps."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import pytest

from sleeper_manager.cloudflare.projection_history import D1ProjectionHistory
from sleeper_manager.projections.direct_baseline import ProjectionBaselineError
from sleeper_manager.projections.live_baseline import (
    DirectBaselineProjectionProvider,
    HistoricalFeatureSlice,
    LiveProjectionTarget,
    ProjectionHistoryError,
)
from tests.sleeper_manager.projections.test_live_baseline import (
    NOW,
    POLICY,
    _row,
    _slice,
    _target,
)


@pytest.mark.parametrize("missing_season", [False, True])
def test_shared_batch_matches_independent_snapshots_in_target_order(missing_season: bool) -> None:
    age = timedelta(days=370 if missing_season else 3)
    rows = _slice(
        _row("old", "401", NOW - age, 20, 30),
        _row("other", "999", NOW - age, 30, 25),
        _row("future", "401", NOW + timedelta(days=2), 100, 30),
    ).rows
    history = D1ProjectionHistory("history-v1", NOW, rows)
    targets = (
        replace(_target(), game_id="later", game_start=NOW + timedelta(days=1)),
        replace(_target(), sleeper_player_id="other", provider_player_id="999"),
        _target(),
    )
    expected = tuple(
        DirectBaselineProjectionProvider(history).project(
            target, scoring_policy=POLICY, decision_time=NOW
        )
        for target in targets
    )
    provider = DirectBaselineProjectionProvider(history)
    with patch.object(
        provider._baseline, "_pregame_index", wraps=provider._baseline._pregame_index
    ) as prepare:
        actual = asyncio.run(
            provider.project_live_batch(
                targets, scoring_policy=POLICY, decision_time=NOW, availability={}
            )
        )
    assert actual == expected
    assert prepare.call_count == 1


def test_target_specific_history_sources_are_not_combined() -> None:
    class TargetHistory:
        def load(self, target: LiveProjectionTarget, *, before) -> HistoricalFeatureSlice:  # type: ignore[no-untyped-def]
            return _slice(
                _row(
                    "old",
                    "401",
                    NOW - timedelta(days=1),
                    40 if target.game_id == "later" else 20,
                    30,
                )
            )

    provider = DirectBaselineProjectionProvider(TargetHistory())
    targets = (_target(), replace(_target(), game_id="later"))
    expected = tuple(
        provider.project(target, scoring_policy=POLICY, decision_time=NOW) for target in targets
    )
    actual = asyncio.run(
        provider.project_live_batch(
            targets, scoring_policy=POLICY, decision_time=NOW, availability={}
        )
    )
    assert actual == expected
    assert actual[0].distribution != actual[1].distribution


def test_missing_season_still_rejects_duplicate_history() -> None:
    row = _slice(_row("old", "401", NOW - timedelta(days=370), 20, 30)).rows[0]
    history = D1ProjectionHistory("history-v1", NOW, (row, row))
    provider = DirectBaselineProjectionProvider(history)
    with pytest.raises(ProjectionBaselineError, match="Duplicate direct history"):
        provider.project(_target(), scoring_policy=POLICY, decision_time=NOW)
    with pytest.raises(ProjectionBaselineError, match="Duplicate direct history"):
        asyncio.run(
            provider.project_live_batch(
                (_target(),), scoring_policy=POLICY, decision_time=NOW, availability={}
            )
        )


def test_started_targets_skip_history_and_naive_decision_time_is_rejected() -> None:
    class UnusedHistory:
        def load(self, target, *, before):  # type: ignore[no-untyped-def]
            raise AssertionError("Started targets must not load history")

    provider = DirectBaselineProjectionProvider(UnusedHistory())
    started = replace(_target(), game_start=NOW - timedelta(seconds=1))
    assert asyncio.run(
        provider.project_live_batch(
            (started,), scoring_policy=POLICY, decision_time=NOW, availability={}
        )
    ) == (None,)
    with pytest.raises(ProjectionHistoryError, match="timezone-aware"):
        asyncio.run(
            provider.project_live_batch(
                (_target(),),
                scoring_policy=POLICY,
                decision_time=NOW.replace(tzinfo=None),
                availability={},
            )
        )
