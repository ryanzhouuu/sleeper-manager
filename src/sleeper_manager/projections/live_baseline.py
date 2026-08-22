"""Live pregame projection adapter behind a replaceable history source."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sleeper_manager.domain.projection import ProjectionSnapshot
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_models import (
    DatasetSourceVersion,
    HistoricalFeatureRow,
)
from sleeper_manager.projections.direct_baseline import (
    MISSING_WARMUP_REASON,
    DirectFantasyPointBaseline,
    PregameProjectionRequest,
    ProjectionBaselineConfig,
    ProjectionBaselineError,
)


@dataclass(frozen=True, slots=True)
class LiveProjectionTarget:
    sleeper_player_id: str
    game_id: str
    game_start: datetime
    provider_player_id: str | None = None

    def __post_init__(self) -> None:
        if not self.sleeper_player_id.strip():
            raise ValueError("Live projection player ID must be non-empty")
        if not self.game_id.strip():
            raise ValueError("Live projection game ID must be non-empty")
        if self.game_start.tzinfo is None:
            raise ValueError("Live projection game starts must be timezone-aware")
        if self.provider_player_id is not None and not self.provider_player_id.strip():
            raise ValueError("Live projection provider player IDs must be non-empty")


@dataclass(frozen=True, slots=True)
class HistoricalFeatureSlice:
    dataset_version: str
    feature_schema_version: str
    source_versions: tuple[DatasetSourceVersion, ...]
    rows: tuple[HistoricalFeatureRow, ...]


class ProjectionHistoryError(ValueError):
    pass


class ProjectionHistoryProvider(Protocol):
    def load(self, target: LiveProjectionTarget, *, before: datetime) -> HistoricalFeatureSlice: ...


class DirectBaselineProjectionProvider:
    """Returns exactly one pregame snapshot per target, or None when none is legal."""

    def __init__(
        self,
        history_provider: ProjectionHistoryProvider,
        *,
        config: ProjectionBaselineConfig | None = None,
    ) -> None:
        self._history_provider = history_provider
        self._baseline = DirectFantasyPointBaseline(config)

    @property
    def model_version(self) -> str:
        return self._baseline.config.model_version

    def project(
        self,
        target: LiveProjectionTarget,
        *,
        scoring_policy: ScoringPolicy,
        decision_time: datetime,
    ) -> ProjectionSnapshot | None:
        if decision_time.tzinfo is None:
            raise ProjectionHistoryError("Live projections require timezone-aware decisions")
        if decision_time > target.game_start:
            return None
        history_slice = self._history_provider.load(target, before=decision_time)
        try:
            request = PregameProjectionRequest(
                dataset_version=history_slice.dataset_version,
                feature_schema_version=history_slice.feature_schema_version,
                player_id=target.sleeper_player_id,
                game_id=target.game_id,
                game_start=target.game_start,
                available_as_of=decision_time,
                history=history_slice.rows,
                history_player_id=target.provider_player_id or target.sleeper_player_id,
                source_versions=history_slice.source_versions,
            )
            return self._baseline.project_pregame(request, scoring_policy=scoring_policy)
        except ProjectionBaselineError as error:
            if error.reason_code == MISSING_WARMUP_REASON:
                return None
            raise


__all__ = (
    "DirectBaselineProjectionProvider",
    "HistoricalFeatureSlice",
    "LiveProjectionTarget",
    "ProjectionHistoryError",
    "ProjectionHistoryProvider",
)
