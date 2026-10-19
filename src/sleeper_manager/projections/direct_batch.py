"""Prepare each shared live history once while retaining the direct model's per-game math."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, cast

from sleeper_manager.domain.projection import ProjectionSnapshot
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.projections.direct_baseline import (
    MISSING_WARMUP_REASON,
    DirectBaselineObservation,
    DirectFantasyPointBaseline,
    PregameProjectionRequest,
    ProjectionBaselineError,
)

if TYPE_CHECKING:
    from sleeper_manager.projections.live_baseline import (
        HistoricalFeatureSlice,
        LiveProjectionTarget,
        ProjectionHistoryProvider,
    )


def project_direct_batch(
    targets: tuple[LiveProjectionTarget, ...],
    *,
    history_provider: ProjectionHistoryProvider,
    baseline: DirectFantasyPointBaseline,
    scoring_policy: ScoringPolicy,
    decision_time: datetime,
) -> tuple[ProjectionSnapshot | None, ...]:
    """Group only identical immutable slices; retain target order and started-game suppression."""
    groups: dict[int, tuple[HistoricalFeatureSlice, list[tuple[int, LiveProjectionTarget]]]] = {}
    snapshots: list[ProjectionSnapshot | None] = [None] * len(targets)
    for index, target in enumerate(targets):
        if decision_time > target.game_start:
            continue
        history = history_provider.load(target, before=decision_time)
        groups.setdefault(id(history), (history, []))[1].append((index, target))
    for history, group in groups.values():
        latest = max((target for _, target in group), key=lambda target: target.game_start)
        prepared = _request(latest, history, decision_time, include_history=True)
        history_index = baseline._pregame_index(prepared, scoring_policy)
        for index, target in group:
            request = _request(target, history, decision_time, include_history=False)
            try:
                snapshots[index] = baseline._project_prepared(
                    request, history_index, scoring_policy=scoring_policy, exceed_score=None
                )
            except ProjectionBaselineError as error:
                if error.reason_code != MISSING_WARMUP_REASON:
                    raise
    return tuple(snapshots)


def _request(
    target: LiveProjectionTarget,
    history: HistoricalFeatureSlice,
    decision_time: datetime,
    *,
    include_history: bool,
) -> PregameProjectionRequest:
    """Validate full history during preparation; per-game metadata uses that prepared index."""
    return PregameProjectionRequest(
        dataset_version=history.dataset_version,
        feature_schema_version=history.feature_schema_version,
        player_id=target.sleeper_player_id,
        game_id=target.game_id,
        game_start=target.game_start,
        available_as_of=decision_time,
        history=cast(tuple[DirectBaselineObservation, ...], history.rows)
        if include_history
        else (),
        history_player_id=target.provider_player_id or target.sleeper_player_id,
        source_versions=history.source_versions,
    )
