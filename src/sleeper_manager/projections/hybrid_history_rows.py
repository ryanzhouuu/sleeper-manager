"""Convert raw regular-season stats without treating normalized defaults as evidence.

Finalization times are supplied by callers; game end and retrieval time are never
substituted. Census joining and artifact export live in hybrid_history_adapter.
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from math import isfinite, isnan

from sleeper_manager.backtesting.artifacts import canonical_json, sha256_text
from sleeper_manager.domain.nba import GameStatus, ScheduledGame
from sleeper_manager.domain.scoring import BoxScoreLine
from sleeper_manager.projections.hybrid_config import OUTCOME_FIELDS, HybridProjectionError
from sleeper_manager.projections.hybrid_types import JointGameObservation, require_aware

SOURCE_STAT_FIELDS = {
    name: "three_point_field_goals_made" if name == "three_pointers_made" else name
    for name in OUTCOME_FIELDS
}


@dataclass(frozen=True, slots=True)
class HistoryFinalization:
    finalized_at: datetime
    source_version: str

    def __post_init__(self) -> None:
        require_aware(self.finalized_at)
        if not self.source_version.strip():
            raise HybridProjectionError("missing_finalization_source")


@dataclass(frozen=True, slots=True)
class HistoryOutcome:
    player_id: str
    game_id: str
    team_id: str
    did_play: bool
    source_version: str

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.player_id, self.game_id, self.team_id, self.source_version)
        ):
            raise HybridProjectionError("missing_outcome_identity")
        if not isinstance(self.did_play, bool):
            raise HybridProjectionError("invalid_participation")


@dataclass(frozen=True, slots=True, order=True)
class HybridHistoryGap:
    game_id: str
    player_id: str
    reason: str
    fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ParsedHybridHistory:
    source_version: str
    observations: tuple[JointGameObservation, ...]
    outcomes: tuple[HistoryOutcome, ...]
    gaps: tuple[HybridHistoryGap, ...]


def history_evidence_version(*evidence: object) -> str:
    """Bind source identities and explicit timing policy into compact row provenance."""
    return "hybrid-history-evidence-v1-" + sha256_text(canonical_json(evidence))


def validate_history_games(
    games: Sequence[ScheduledGame], finalization: Mapping[str, HistoryFinalization]
) -> dict[str, ScheduledGame]:
    """Reject ambiguous schedules and timing evidence incompatible with final games."""
    by_game = {game.provider_id: game for game in games}
    if len(by_game) != len(games):
        raise HybridProjectionError("duplicate_history_game")
    for game in games:
        require_aware(game.start_time)
        if not game.provider_id.strip() or game.home_team_id == game.away_team_id:
            raise HybridProjectionError("invalid_history_game")
    for game_id, evidence in finalization.items():
        final_game = by_game.get(game_id)
        if final_game is None or final_game.status is not GameStatus.FINAL:
            raise HybridProjectionError("finalization_without_final_game")
        if evidence.finalized_at < final_game.start_time:
            raise HybridProjectionError("finalization_before_game")
    return by_game


def parse_hybrid_history_rows(
    rows: Iterable[Mapping[str, object]],
    *,
    games: Sequence[ScheduledGame],
    resolved_player_ids: Collection[str],
    finalization: Mapping[str, HistoryFinalization],
    source_version: str,
) -> ParsedHybridHistory:
    """Read SportsDataverse columns; incomplete evidence stays visible as gaps.

    Only season_type 2 is admitted. Non-null stats must be nonnegative integers;
    participation must be a source boolean. DNPs supply outcomes, never played samples.
    """
    if not source_version.strip() or any(not player.strip() for player in resolved_player_ids):
        raise HybridProjectionError("missing_history_source_or_identity")
    by_game = validate_history_games(games, finalization)
    resolved = set(resolved_player_ids)
    observations: list[JointGameObservation] = []
    outcomes: list[HistoryOutcome] = []
    gaps: list[HybridHistoryGap] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if _number(row.get("season_type"), integer=True) != 2:
            continue
        game_id, player_id, team_id = (
            _identity(row.get(name)) for name in ("game_id", "athlete_id", "team_id")
        )
        key = game_id, player_id
        if key in seen:
            raise HybridProjectionError("duplicate_player_game")
        seen.add(key)
        if not all((game_id, player_id, team_id)):
            gaps.append(HybridHistoryGap(game_id, player_id, "missing_source_identity"))
            continue
        game = by_game.get(game_id)
        if game is None or game.status is not GameStatus.FINAL:
            gaps.append(HybridHistoryGap(game_id, player_id, "missing_final_game"))
            continue
        if team_id not in (game.home_team_id, game.away_team_id):
            raise HybridProjectionError("outcome_team_mismatch")
        if player_id not in resolved:
            gaps.append(HybridHistoryGap(game_id, player_id, "unresolved_player"))
            continue
        dnp = row.get("did_not_play")
        if _missing(dnp):
            gaps.append(HybridHistoryGap(game_id, player_id, "missing_participation"))
            continue
        if not isinstance(dnp, bool):
            raise HybridProjectionError("invalid_participation")
        outcomes.append(HistoryOutcome(player_id, game_id, team_id, not dnp, source_version))
        timing = finalization.get(game_id)
        if timing is None:
            gaps.append(HybridHistoryGap(game_id, player_id, "missing_finalization"))
        if dnp:
            continue
        values = {
            field: int(_number(row[column], integer=True))
            for field, column in SOURCE_STAT_FIELDS.items()
            if not _missing(row.get(column))
        }
        missing_fields = tuple(sorted(set(OUTCOME_FIELDS) - values.keys()))
        if missing_fields:
            gaps.append(HybridHistoryGap(game_id, player_id, "missing_stat_fields", missing_fields))
        minutes = None if _missing(row.get("minutes")) else _number(row["minutes"])
        if minutes is None or minutes <= 0:
            gaps.append(HybridHistoryGap(game_id, player_id, "missing_positive_minutes"))
        if timing is not None:
            observations.append(
                JointGameObservation(
                    player_id,
                    game_id,
                    game.start_time,
                    timing.finalized_at,
                    True,
                    minutes,
                    BoxScoreLine(**values),
                    tuple(values),
                    history_evidence_version(source_version, timing.source_version),
                )
            )
    return ParsedHybridHistory(
        source_version,
        tuple(sorted(observations, key=lambda row: (row.game_start, row.game_id, row.player_id))),
        tuple(sorted(outcomes, key=lambda row: (row.game_id, row.player_id))),
        tuple(sorted(gaps)),
    )


def _missing(value: object) -> bool:
    return value is None or (isinstance(value, float) and isnan(value))


def _number(value: object, *, integer: bool = False) -> float:
    """Reject booleans, strings, infinities and fractional counts rather than coercing them."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise HybridProjectionError("invalid_history_number")
    if not isfinite(value) or value < 0 or (integer and int(value) != value):
        raise HybridProjectionError("invalid_history_number")
    return float(value)


def _identity(value: object) -> str:
    """Accept raw numeric IDs without appending '.0'; missing identity remains a gap."""
    if _missing(value):
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(int(_number(value, integer=True)))
