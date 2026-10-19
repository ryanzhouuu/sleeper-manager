"""Avoid repeated full-history preparation while preserving cutoff filtering and warmup gaps."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import pytest

from sleeper_manager.cloudflare.projection_history import D1ProjectionHistory
from sleeper_manager.projections.direct_baseline import ProjectionBaselineError
from sleeper_manager.projections.live_baseline import DirectBaselineProjectionProvider
from tests.sleeper_manager.projections.test_live_baseline import (
    NOW,
    POLICY,
    _row,
    _slice,
    _target,
)


def test_same_cutoff_shares_validated_slice_and_changed_cutoff_filters_again() -> None:
    history = D1ProjectionHistory(
        "history-v1", NOW, _slice(_row("old", "401", NOW - timedelta(days=1), 20, 30)).rows
    )
    first = history.load(_target(), before=NOW)
    other = replace(_target(), game_id="other", provider_player_id="other")
    assert history.load(other, before=NOW) is first
    earlier = history.load(_target(), before=NOW - timedelta(days=2))
    assert earlier.rows == ()
    assert history.load(_target(), before=NOW) == first


def test_history_replacement_cannot_return_a_stale_prepared_slice() -> None:
    history = D1ProjectionHistory(
        "history-v1", NOW, _slice(_row("old", "401", NOW - timedelta(days=1), 20, 30)).rows
    )
    first = history.load(_target(), before=NOW)
    changed = replace(
        history,
        observations=_slice(_row("new", "401", NOW - timedelta(days=1), 40, 30)).rows,
        history_version="history-v2",
    )
    second = changed.load(_target(), before=NOW)
    assert second.dataset_version == "history-v2"
    assert second.rows != first.rows


def test_missing_season_prepares_once_without_suppressing_metadata_validation() -> None:
    history = D1ProjectionHistory(
        "history-v1", NOW, _slice(_row("old", "401", NOW - timedelta(days=370), 20, 30)).rows
    )
    provider = DirectBaselineProjectionProvider(history)
    with patch.object(
        provider._baseline, "_pregame_index", wraps=provider._baseline._pregame_index
    ) as prepare:
        snapshots = asyncio.run(
            provider.project_live_batch(
                (_target(), replace(_target(), game_id="later")),
                scoring_policy=POLICY,
                decision_time=NOW,
                availability={},
            )
        )
    assert snapshots == (None, None)
    assert prepare.call_count == 1
    assert len(history.load(_target(), before=NOW).rows) == 1
    with pytest.raises(ProjectionBaselineError, match="dataset_version"):
        asyncio.run(
            DirectBaselineProjectionProvider(
                replace(history, history_version="")
            ).project_live_batch(
                (_target(),), scoring_policy=POLICY, decision_time=NOW, availability={}
            )
        )


def test_in_season_history_keeps_other_seasons_for_identical_provenance() -> None:
    rows = _slice(
        _row("older", "401", NOW - timedelta(days=370), 50, 30),
        _row("current", "401", NOW - timedelta(days=1), 20, 30),
    ).rows
    history = D1ProjectionHistory("history-v1", NOW, rows)
    provider = DirectBaselineProjectionProvider(history)
    with patch.object(
        provider._baseline, "project_pregame", wraps=provider._baseline.project_pregame
    ) as project:
        snapshot = provider.project(_target(), scoring_policy=POLICY, decision_time=NOW)
    assert snapshot is not None
    assert len(project.call_args.args[0].history) == 2
    assert snapshot == DirectBaselineProjectionProvider(history).project(
        _target(), scoring_policy=POLICY, decision_time=NOW
    )
