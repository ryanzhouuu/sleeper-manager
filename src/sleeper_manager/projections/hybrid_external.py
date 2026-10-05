"""Qualify archived external centers without promoting provider timestamps to expiry rules."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sleeper_manager.domain.forecast_capture import ForecastRetrievalResult, ForecastRetrievalStatus
from sleeper_manager.domain.nba_season import nba_season_start_year
from sleeper_manager.integrations.sleeper.forecast_fetch import season_forecast_source
from sleeper_manager.projections.hybrid_config import CORE_STATS, HybridProjectionConfig


@dataclass(frozen=True, slots=True)
class ExternalCenter:
    values: tuple[float, ...] | None
    rejection: str | None


def qualify_external_center(
    retrieval: ForecastRetrievalResult | None,
    *,
    player_id: str,
    cutoff: datetime,
    game_start: datetime,
    config: HybridProjectionConfig,
) -> ExternalCenter:
    """Accept only this adapter's complete, coherent, recently observed same-season row."""
    if not config.use_external:
        return ExternalCenter(None, "internal_only")
    if retrieval is None:
        return ExternalCenter(None, "missing_forecast_receipt")
    if retrieval.cutoff != cutoff or retrieval.player_id != player_id:
        return ExternalCenter(None, "forecast_request_mismatch")
    if retrieval.status is not ForecastRetrievalStatus.AVAILABLE:
        return ExternalCenter(None, f"forecast_{retrieval.status.value}")
    assert retrieval.provenance is not None and retrieval.forecast is not None
    expected = season_forecast_source(str(nba_season_start_year(game_start)), "regular")
    if retrieval.provenance.source != expected or retrieval.forecast.company != "rotowire":
        return ExternalCenter(None, "forecast_source_mismatch")
    if cutoff - retrieval.provenance.persisted_at > timedelta(hours=config.forecast_max_age_hours):
        return ExternalCenter(None, "stale_forecast_receipt")
    supplied = tuple(retrieval.forecast.stat(stat) for stat in CORE_STATS)
    if any(value is None or value < 0 for value in supplied):
        return ExternalCenter(None, "incomplete_or_negative_forecast_stats")
    values = tuple(float(value) for value in supplied if value is not None)
    if values[0] < 3 * values[6]:
        return ExternalCenter(None, "incoherent_forecast_stats")
    return ExternalCenter(values, None)
