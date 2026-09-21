"""Compute provider-neutral identities for normalized forecast revision surfaces."""

from __future__ import annotations

import json
from collections.abc import Sequence
from hashlib import sha256

from sleeper_manager.domain.forecast_capture import (
    ForecastCaptureError,
    ForecastSource,
    NormalizedPlayerForecast,
)


def forecast_semantic_hash(
    source: ForecastSource,
    records: Sequence[NormalizedPlayerForecast],
) -> str:
    """Hash only source and player fields whose changes create a revision."""

    supplied = tuple(records)
    if any(not isinstance(record, NormalizedPlayerForecast) for record in supplied):
        raise ForecastCaptureError("Forecast semantic records must be normalized")
    ordered = tuple(sorted(supplied, key=lambda record: record.player_id))
    player_ids = tuple(record.player_id for record in ordered)
    if len(set(player_ids)) != len(player_ids):
        raise ForecastCaptureError("Forecast semantic records contain duplicate player IDs")
    value = {
        "source": {
            "provider": source.provider,
            "endpoint": source.endpoint,
            "season": source.season,
            "season_type": source.season_type,
            "horizon": source.horizon,
            "adapter_version": source.adapter_version,
        },
        "records": [
            {
                "player_id": record.player_id,
                "company": record.company,
                "team_id": record.team_id,
                "stats": record.stats,
            }
            for record in ordered
        ],
    }
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


__all__ = ("forecast_semantic_hash",)
