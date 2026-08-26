from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sleeper_manager.domain.scoring import BoxScoreLine
from sleeper_manager.integrations.nba.historical_feature_models import DatasetSourceVersion
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository
from sleeper_manager.projections.direct_baseline import DirectBaselineObservation
from sleeper_manager.projections.live_baseline import (
    HistoricalFeatureSlice,
    LiveProjectionTarget,
    ProjectionHistoryError,
)

_BOX_SCORE_FIELDS = frozenset(BoxScoreLine.__dataclass_fields__)


@dataclass(frozen=True, slots=True)
class D1ProjectionHistory:
    history_version: str
    loaded_at: datetime
    observations: tuple[DirectBaselineObservation, ...]

    @classmethod
    async def load_version(
        cls,
        repository: AsyncRuntimeStateRepository,
        *,
        history_version: str,
        loaded_at: datetime,
    ) -> D1ProjectionHistory:
        records = await repository.load_projection_observations(
            history_version,
            before=loaded_at,
        )
        if not records:
            raise ProjectionHistoryError("Projection history version has no observations")
        observations = tuple(
            DirectBaselineObservation(
                player_id=record.player_id,
                game_id=record.game_id,
                game_start=record.game_start,
                outcome_finalized_at=record.outcome_finalized_at,
                minutes=record.minutes,
                started=record.started,
                did_not_play=record.did_not_play,
                box_score=_box_score(record.box_score_json),
                source_version=record.source_version,
            )
            for record in records
        )
        return cls(history_version, loaded_at, observations)

    def load(self, target: LiveProjectionTarget, *, before: datetime) -> HistoricalFeatureSlice:
        del target
        return HistoricalFeatureSlice(
            dataset_version=self.history_version,
            feature_schema_version="direct-baseline-observation-v1",
            source_versions=(
                DatasetSourceVersion(
                    provider="d1-projection-history",
                    schema_version="1",
                    source_ids=(self.history_version,),
                ),
            ),
            rows=tuple(
                observation
                for observation in self.observations
                if observation.game_start < before
                and observation.outcome_finalized_at is not None
                and observation.outcome_finalized_at <= before
            ),
        )


def _box_score(payload_json: str) -> BoxScoreLine:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as error:
        raise ProjectionHistoryError(
            "Projection history contains invalid box-score JSON"
        ) from error
    if not isinstance(payload, Mapping):
        raise ProjectionHistoryError("Projection history box scores must be JSON objects")
    extras = set(payload) - _BOX_SCORE_FIELDS
    if extras:
        raise ProjectionHistoryError(
            "Projection history contains unknown box-score fields: "
            + ", ".join(sorted(str(item) for item in extras))
        )
    values: dict[str, int] = {}
    for field in _BOX_SCORE_FIELDS:
        value: Any = payload.get(field, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProjectionHistoryError(
                f"Projection history box-score field {field!r} must be an integer"
            )
        values[field] = value
    return BoxScoreLine(**values)


__all__ = ("D1ProjectionHistory",)
