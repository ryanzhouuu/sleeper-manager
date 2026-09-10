"""Decode canonical team-week JSON into typed replay evidence.

All helpers fail closed at the persisted-data boundary. File access, bundle
layout, and cross-record identity checks remain in `artifact`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sleeper_manager.backtesting.replay.inputs.models import (
    HistoricalTeamWeekInput,
    ReplayCoverageSummary,
    ReplayInputExclusion,
)
from sleeper_manager.backtesting.replay.models import (
    ReplayGame,
    ReplayGameStatus,
    ReplayPlayerGame,
)
from sleeper_manager.domain.planning import PlanningQuality, PlanningReasonCode
from sleeper_manager.domain.projection import (
    ProjectionAdjustmentKind,
    ProjectionComponent,
    ProjectionDistribution,
    ProjectionFallback,
    ProjectionReason,
    ProjectionSnapshot,
)


class HistoricalTeamWeekArtifactError(ValueError):
    """Raised when a persisted team-week artifact cannot be reconstructed safely."""


def decode_historical_team_week(payload: dict[str, Any]) -> HistoricalTeamWeekInput:
    """Decode one canonical payload without accepting missing decision-critical fields."""

    coverage = _coverage(_require_mapping(payload, "coverage"))
    exclusions = tuple(_exclusion(item) for item in _require_list(payload, "exclusions"))
    games = tuple(_game(item) for item in _require_list(payload, "games"))
    player_games = tuple(_player_game(item) for item in _require_list(payload, "player_games"))
    team_week = HistoricalTeamWeekInput(
        manifest_id=_require_str(payload, "manifest_id"),
        league_id=_require_str(payload, "league_id"),
        season=_require_str(payload, "season"),
        week=_require_int(payload, "week"),
        roster_id=_require_int(payload, "roster_id"),
        starter_slots=tuple(
            _require_text_item(item, "starter_slots")
            for item in _require_list(payload, "starter_slots")
        ),
        roster_player_ids=tuple(
            _require_text_item(item, "roster_player_ids")
            for item in _require_list(payload, "roster_player_ids")
        ),
        observed_starter_ids=tuple(
            _optional_text_item(item, "observed_starter_ids")
            for item in _require_list(payload, "observed_starter_ids")
        ),
        games=games,
        player_games=player_games,
        eligibility_quality=_enum(
            PlanningQuality, _require_str(payload, "eligibility_quality"), "eligibility_quality"
        ),
        coverage=coverage,
        exclusions=exclusions,
    )
    if "complete" in payload:
        if not isinstance(payload["complete"], bool):
            raise HistoricalTeamWeekArtifactError("complete must be a boolean when present")
        if payload["complete"] != team_week.complete:
            raise HistoricalTeamWeekArtifactError(
                "Persisted complete flag does not match reconstructed coverage"
            )
    return team_week


def _coverage(payload: dict[str, Any]) -> ReplayCoverageSummary:
    """Decode coverage counts and typed missing-evidence reasons."""

    missing_raw = _require_list(payload, "missing_evidence")
    missing: list[tuple[PlanningReasonCode, int]] = []
    for item in missing_raw:
        if not isinstance(item, list | tuple) or len(item) != 2:
            raise HistoricalTeamWeekArtifactError(
                "Coverage missing_evidence entries must be [reason, count] pairs"
            )
        reason = _enum(PlanningReasonCode, item[0], "missing_evidence.reason")
        if not isinstance(item[1], int) or isinstance(item[1], bool):
            raise HistoricalTeamWeekArtifactError(
                "Coverage missing_evidence counts must be integers"
            )
        missing.append((reason, item[1]))
    return ReplayCoverageSummary(
        expected_player_games=_require_int(payload, "expected_player_games"),
        joined_player_games=_require_int(payload, "joined_player_games"),
        resolved_identities=_require_int(payload, "resolved_identities"),
        exact_eligibility=_require_int(payload, "exact_eligibility"),
        best_known_eligibility=_require_int(payload, "best_known_eligibility"),
        scored_player_games=_require_int(payload, "scored_player_games"),
        missing_evidence=tuple(missing),
        projected_player_games=_require_int(payload, "projected_player_games"),
        inferred_team_membership=_require_int(
            {"inferred_team_membership": 0, **payload}, "inferred_team_membership"
        ),
    )


def _exclusion(payload: object) -> ReplayInputExclusion:
    """Decode one structured replay-input exclusion."""

    mapping = _as_mapping(payload, "exclusion")
    return ReplayInputExclusion(
        reason=_enum(PlanningReasonCode, _require_str(mapping, "reason"), "exclusion.reason"),
        scope=_require_str(mapping, "scope"),
        detail=_require_str(mapping, "detail"),
    )


def _game(payload: object) -> ReplayGame:
    """Decode one replay game with an aware start timestamp."""

    mapping = _as_mapping(payload, "game")
    team_ids_raw = _require_list(mapping, "team_ids")
    if len(team_ids_raw) != 2:
        raise HistoricalTeamWeekArtifactError("Replay games require exactly two team_ids")
    team_ids = (
        _require_text_item(team_ids_raw[0], "team_ids"),
        _require_text_item(team_ids_raw[1], "team_ids"),
    )
    final_raw = mapping.get("final_time")
    return ReplayGame(
        game_id=_require_str(mapping, "game_id"),
        start_time=_require_aware_datetime(mapping, "start_time"),
        final_time=None if final_raw is None else _aware_datetime(final_raw, "final_time"),
        week=_require_int(mapping, "week"),
        team_ids=team_ids,
        status=_enum(ReplayGameStatus, _require_str(mapping, "status"), "status"),
    )


def _player_game(payload: object) -> ReplayPlayerGame:
    """Decode one rostered player-game and its optional projection."""

    mapping = _as_mapping(payload, "player_game")
    projection_raw = mapping.get("projection")
    if "projection" not in mapping:
        raise HistoricalTeamWeekArtifactError("player_game.projection is required")
    projection = None if projection_raw is None else decode_projection_snapshot(projection_raw)
    membership = mapping.get("membership_segment")
    if "membership_segment" not in mapping:
        raise HistoricalTeamWeekArtifactError("player_game.membership_segment is required")
    if membership is not None and not isinstance(membership, str):
        raise HistoricalTeamWeekArtifactError("player_game.membership_segment must be a string")
    return ReplayPlayerGame(
        sleeper_id=_require_str(mapping, "sleeper_id"),
        provider_player_id=_require_str(mapping, "provider_player_id"),
        game_id=_require_str(mapping, "game_id"),
        fantasy_team_id=_require_int(mapping, "fantasy_team_id"),
        rostered_at_tipoff=_require_bool(mapping, "rostered_at_tipoff"),
        eligible_positions=tuple(
            _require_text_item(item, "eligible_positions")
            for item in _require_list(mapping, "eligible_positions")
        ),
        actual_score=_require_float(mapping, "actual_score"),
        projection=projection,
        membership_segment=membership,
    )


def decode_projection_snapshot(payload: object) -> ProjectionSnapshot:
    """Decode one point-in-time projection snapshot."""

    mapping = _as_mapping(payload, "projection")
    return ProjectionSnapshot(
        player_id=_require_str(mapping, "player_id"),
        game_id=_require_str(mapping, "game_id"),
        available_as_of=_require_aware_datetime(mapping, "available_as_of"),
        model_version=_require_str(mapping, "model_version"),
        input_version=_require_str(mapping, "input_version"),
        scoring_policy_version=_require_str(mapping, "scoring_policy_version"),
        distribution=_distribution(_require_mapping(mapping, "distribution")),
        reasons=tuple(_reason(item) for item in _require_list(mapping, "reasons")),
        components=tuple(_component(item) for item in _require_list(mapping, "components")),
    )


def _distribution(payload: dict[str, Any]) -> ProjectionDistribution:
    """Decode a projection distribution while preserving weighted evidence."""

    percentiles_raw = _require_list(payload, "percentiles")
    percentiles: list[tuple[int, float]] = []
    for item in percentiles_raw:
        if not isinstance(item, list | tuple) or len(item) != 2:
            raise HistoricalTeamWeekArtifactError(
                "Projection percentiles must be [percentile, value] pairs"
            )
        if not isinstance(item[0], int) or isinstance(item[0], bool):
            raise HistoricalTeamWeekArtifactError("Projection percentile keys must be integers")
        percentiles.append((item[0], _as_float(item[1], "percentiles.value")))
    observations_raw = _require_list(payload, "weighted_observations")
    observations: list[tuple[float, float]] = []
    for item in observations_raw:
        if not isinstance(item, list | tuple) or len(item) != 2:
            raise HistoricalTeamWeekArtifactError(
                "Projection weighted_observations must be [value, weight] pairs"
            )
        observations.append(
            (_as_float(item[0], "weighted_observations.value"), _as_float(item[1], "weight"))
        )
    exceedance = payload.get("exceedance_score")
    probability = payload.get("probability_exceeding_score")
    if "exceedance_score" not in payload or "probability_exceeding_score" not in payload:
        raise HistoricalTeamWeekArtifactError(
            "Projection distribution requires exceedance_score and probability_exceeding_score"
        )
    return ProjectionDistribution(
        expected_value=_require_float(payload, "expected_value"),
        median=_require_float(payload, "median"),
        percentiles=tuple(percentiles),
        lower_bound=_require_float(payload, "lower_bound"),
        upper_bound=_require_float(payload, "upper_bound"),
        variance=_require_float(payload, "variance"),
        weighted_observations=tuple(observations),
        exceedance_score=None if exceedance is None else _as_float(exceedance, "exceedance_score"),
        probability_exceeding_score=(
            None if probability is None else _as_float(probability, "probability_exceeding_score")
        ),
    )


def _reason(payload: object) -> ProjectionReason:
    """Decode one human-readable projection reason."""

    mapping = _as_mapping(payload, "projection.reason")
    adjustment = mapping.get("adjustment")
    if "adjustment" not in mapping:
        raise HistoricalTeamWeekArtifactError("projection.reason.adjustment is required")
    return ProjectionReason(
        code=_require_str(mapping, "code"),
        message=_require_str(mapping, "message"),
        applied=_require_bool(mapping, "applied"),
        adjustment=None if adjustment is None else _as_float(adjustment, "adjustment"),
    )


def _component(payload: object) -> ProjectionComponent:
    """Decode one typed projection component and adjustment."""

    mapping = _as_mapping(payload, "projection.component")
    baseline = mapping.get("baseline")
    adjustment = mapping.get("adjustment")
    for field in ("baseline", "adjustment"):
        if field not in mapping:
            raise HistoricalTeamWeekArtifactError(f"projection.component.{field} is required")
    return ProjectionComponent(
        code=_require_str(mapping, "code"),
        estimate=_require_float(mapping, "estimate"),
        baseline=None if baseline is None else _as_float(baseline, "baseline"),
        adjustment=None if adjustment is None else _as_float(adjustment, "adjustment"),
        kind=_enum(
            ProjectionAdjustmentKind, _require_str(mapping, "kind"), "projection.component.kind"
        ),
        effective_sample=_require_float(mapping, "effective_sample"),
        fallback=_enum(
            ProjectionFallback,
            _require_str(mapping, "fallback"),
            "projection.component.fallback",
        ),
        message=_require_str(mapping, "message"),
    )


def _enum(enum_type: type[Any], value: object, field: str) -> Any:
    """Require a valid string value for the requested enum type."""

    if not isinstance(value, str):
        raise HistoricalTeamWeekArtifactError(f"{field} must be a string enum value")
    try:
        return enum_type(value)
    except ValueError as error:
        raise HistoricalTeamWeekArtifactError(f"Invalid {field} value {value!r}") from error


def _require_mapping(payload: dict[str, Any], field: str) -> dict[str, Any]:
    """Require a present object-valued field."""

    if field not in payload:
        raise HistoricalTeamWeekArtifactError(f"Missing required field {field}")
    return _as_mapping(payload[field], field)


def _as_mapping(payload: object, field: str) -> dict[str, Any]:
    """Validate an already-selected value as a JSON object."""

    if not isinstance(payload, dict):
        raise HistoricalTeamWeekArtifactError(f"{field} must be a JSON object")
    return payload


def _require_list(payload: dict[str, Any], field: str) -> list[Any]:
    """Require a present array-valued field."""

    if field not in payload:
        raise HistoricalTeamWeekArtifactError(f"Missing required field {field}")
    value = payload[field]
    if not isinstance(value, list):
        raise HistoricalTeamWeekArtifactError(f"{field} must be a JSON array")
    return value


def _require_str(payload: dict[str, Any], field: str) -> str:
    """Require a present non-empty string field."""

    if field not in payload:
        raise HistoricalTeamWeekArtifactError(f"Missing required field {field}")
    value = payload[field]
    if not isinstance(value, str) or not value.strip():
        raise HistoricalTeamWeekArtifactError(f"{field} must be a non-empty string")
    return value


def _require_text_item(value: object, field: str) -> str:
    """Validate one non-empty string within an array field."""

    if not isinstance(value, str) or not value.strip():
        raise HistoricalTeamWeekArtifactError(f"{field} entries must be non-empty strings")
    return value


def _optional_text_item(value: object, field: str) -> str | None:
    """Validate an optional non-empty string within an array field."""

    if value is None:
        return None
    return _require_text_item(value, field)


def _require_int(payload: dict[str, Any], field: str) -> int:
    """Require an integer field without accepting booleans."""

    if field not in payload:
        raise HistoricalTeamWeekArtifactError(f"Missing required field {field}")
    value = payload[field]
    if not isinstance(value, int) or isinstance(value, bool):
        raise HistoricalTeamWeekArtifactError(f"{field} must be an integer")
    return value


def _require_bool(payload: dict[str, Any], field: str) -> bool:
    """Require a boolean-valued field."""

    if field not in payload:
        raise HistoricalTeamWeekArtifactError(f"Missing required field {field}")
    value = payload[field]
    if not isinstance(value, bool):
        raise HistoricalTeamWeekArtifactError(f"{field} must be a boolean")
    return value


def _require_float(payload: dict[str, Any], field: str) -> float:
    """Require a present numeric field and normalize it to float."""

    if field not in payload:
        raise HistoricalTeamWeekArtifactError(f"Missing required field {field}")
    return _as_float(payload[field], field)


def _as_float(value: object, field: str) -> float:
    """Validate an already-selected numeric value without accepting booleans."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise HistoricalTeamWeekArtifactError(f"{field} must be a number")
    return float(value)


def _require_aware_datetime(payload: dict[str, Any], field: str) -> datetime:
    """Require a present timezone-aware timestamp field."""

    if field not in payload:
        raise HistoricalTeamWeekArtifactError(f"Missing required field {field}")
    return _aware_datetime(payload[field], field)


def _aware_datetime(value: object, field: str) -> datetime:
    """Parse an ISO-8601 timestamp and reject naive values."""

    if not isinstance(value, str) or not value.strip():
        raise HistoricalTeamWeekArtifactError(f"{field} must be an ISO-8601 timestamp string")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise HistoricalTeamWeekArtifactError(f"Malformed {field} timestamp {value!r}") from error
    if parsed.tzinfo is None:
        raise HistoricalTeamWeekArtifactError(f"{field} timestamp must include a timezone")
    return parsed
