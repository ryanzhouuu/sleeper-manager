from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from sleeper_manager.domain.projection import (
    ProjectionAdjustmentKind,
    ProjectionComponent,
    ProjectionFallback,
)


class OpportunityModelError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OpportunityModelConfig:
    recency_half_life_days: float = 14.0
    participation_prior_strength: float = 4.0
    production_shrinkage_minutes: float = 120.0
    pace_clip: tuple[float, float] = (0.92, 1.08)
    percentiles: tuple[int, ...] = (10, 25, 50, 75, 90)
    disable_pace: bool = False
    disable_defense: bool = False

    def __post_init__(self) -> None:
        if not isfinite(self.recency_half_life_days) or self.recency_half_life_days <= 0:
            raise OpportunityModelError("Recency half-life must be finite and positive")
        if self.participation_prior_strength < 0 or not isfinite(self.participation_prior_strength):
            raise OpportunityModelError("Participation prior strength must be non-negative")
        if self.production_shrinkage_minutes < 0 or not isfinite(self.production_shrinkage_minutes):
            raise OpportunityModelError("Production shrinkage must be non-negative")
        if len(self.pace_clip) != 2 or not 0 < self.pace_clip[0] <= self.pace_clip[1]:
            raise OpportunityModelError("Pace clip must be an ordered positive range")
        if tuple(sorted(set(self.percentiles))) != self.percentiles:
            raise OpportunityModelError("Projection percentiles must be unique and ordered")

    @property
    def model_version(self) -> str:
        payload = {
            "recency_half_life_days": self.recency_half_life_days,
            "participation_prior_strength": self.participation_prior_strength,
            "production_shrinkage_minutes": self.production_shrinkage_minutes,
            "pace_clip": self.pace_clip,
            "percentiles": self.percentiles,
            "disable_pace": self.disable_pace,
            "disable_defense": self.disable_defense,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]
        return f"opportunity-v3-{digest}"


@dataclass(frozen=True, slots=True)
class _Estimate:
    value: float
    baseline: float | None
    adjustment: float | None
    kind: ProjectionAdjustmentKind
    effective_sample: float
    fallback: ProjectionFallback
    message: str

    def component(self, code: str) -> ProjectionComponent:
        return ProjectionComponent(
            code=code,
            estimate=round(self.value, 6),
            baseline=None if self.baseline is None else round(self.baseline, 6),
            adjustment=None if self.adjustment is None else round(self.adjustment, 6),
            kind=self.kind,
            effective_sample=round(self.effective_sample, 6),
            fallback=self.fallback,
            message=self.message,
        )


@dataclass(frozen=True, slots=True)
class _LeagueProductionPrior:
    independent_rate: float | None
    shared_rate: float | None
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _ProductionPrefix:
    game_start: datetime
    score: float
    minutes: float
