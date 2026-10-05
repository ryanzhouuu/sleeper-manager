"""Archive-backed candidate provider compatible with the existing live target boundary."""

from collections.abc import Mapping
from datetime import datetime

from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.domain.projection import ProjectionSnapshot
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.integrations.sleeper.forecast_fetch import season_forecast_source
from sleeper_manager.persistence.forecast_repository import ForecastArchiveRepository
from sleeper_manager.projections.hybrid_config import HybridProjectionConfig, HybridProjectionError
from sleeper_manager.projections.hybrid_model import HybridProjectionBatch, HybridProjectionResult
from sleeper_manager.projections.hybrid_types import GameAvailability, HybridHistory, require_aware
from sleeper_manager.projections.live_baseline import LiveProjectionTarget
from sleeper_manager.workflows.forecast_reader import read_player_forecasts


class HybridProjectionProvider:
    """Batch both rosters at a cutoff; configuration never switches active advice."""

    def __init__(
        self,
        history: HybridHistory,
        *,
        archive: ForecastArchiveRepository | None = None,
        config: HybridProjectionConfig | None = None,
    ) -> None:
        self.history = history
        self.archive = archive
        self.config = config or HybridProjectionConfig()

    @property
    def model_version(self) -> str:
        return self.config.model_version

    def project_batch(
        self,
        targets: tuple[LiveProjectionTarget, ...],
        *,
        scoring_policy: ScoringPolicy,
        decision_time: datetime,
        availability: Mapping[tuple[str, str], GameAvailability] | None = None,
    ) -> tuple[HybridProjectionResult, ...]:
        """Prepare history and read one archived revision per season, outside scenario loops."""
        require_aware(decision_time)
        keys = tuple((target.sleeper_player_id, target.game_id) for target in targets)
        if len(set(keys)) != len(keys):
            raise HybridProjectionError("duplicate_projection_target")
        results: dict[tuple[str, str], HybridProjectionResult] = {}
        seasons = sorted({nba_season_start_year(target.game_start) for target in targets})
        for season in seasons:
            group = tuple(
                target for target in targets if nba_season_start_year(target.game_start) == season
            )
            prepared = HybridProjectionBatch(
                self.history,
                cutoff=decision_time,
                game_start=group[0].game_start,
                scoring_policy=scoring_policy,
                config=self.config,
            )
            reads = {}
            if self.archive is not None and self.config.use_external:
                source = season_forecast_source(str(season), "regular")
                if self.archive.load_latest_receipt(source, cutoff=decision_time) is not None:
                    players = tuple(sorted({target.sleeper_player_id for target in group}))
                    reads = {
                        read.player_id: read
                        for read in read_player_forecasts(
                            self.archive, source, players, decision_time
                        )
                    }
            for target in group:
                key = target.sleeper_player_id, target.game_id
                results[key] = prepared.project(
                    target,
                    forecast=reads.get(target.sleeper_player_id),
                    availability=(availability or {}).get(key),
                )
        return tuple(results[key] for key in keys)

    def project(
        self,
        target: LiveProjectionTarget,
        *,
        scoring_policy: ScoringPolicy,
        decision_time: datetime,
    ) -> ProjectionSnapshot | None:
        """Existing planner-compatible adapter; shadow callers should retain batch evidence."""
        return self.project_batch(
            (target,), scoring_policy=scoring_policy, decision_time=decision_time
        )[0].projection
