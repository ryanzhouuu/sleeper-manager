from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from sleeper_manager.backtesting.artifacts import canonical_json, canonicalize, sha256_text
from sleeper_manager.backtesting.replay.engine import ReplayConfig
from sleeper_manager.backtesting.replay.models import ReplayGame, ReplayGameStatus, ReplayPlayerGame
from sleeper_manager.backtesting.replay.planning_adapter import team_week_state_from_replay
from sleeper_manager.backtesting.replay.state import ReplayState
from sleeper_manager.domain.league import LeagueProfile
from sleeper_manager.domain.planning import PlanningReasonCode, TeamWeekState


class ReplayRunnerError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        reason: PlanningReasonCode = PlanningReasonCode.AMBIGUOUS_EVENT_ORDER,
    ) -> None:
        super().__init__(message)
        self.reason = reason


class ReplayEventKind(StrEnum):
    TRANSACTION_EFFECT = "transaction_effect"
    PLANNING_CUTOFF = "planning_cutoff"
    TIPOFF_BATCH = "tipoff_batch"
    GAME_FINALIZATION = "game_finalization"
    WEEK_END = "week_end"


@dataclass(frozen=True, slots=True)
class ReplayTransaction:
    transaction_id: str
    effective_at: datetime
    roster_id: int
    adds: tuple[str, ...] = ()
    drops: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.transaction_id, "Replay transaction ID")
        _require_aware(self.effective_at, "Replay transaction time")
        if self.roster_id <= 0:
            raise ReplayRunnerError(
                "Replay transaction roster IDs must be positive",
                reason=PlanningReasonCode.ROSTER_STATE_MISMATCH,
            )
        adds = _normalize_ids(self.adds)
        drops = _normalize_ids(self.drops)
        if set(adds) & set(drops):
            raise ReplayRunnerError(
                "A replay transaction cannot add and drop the same player",
                reason=PlanningReasonCode.ROSTER_STATE_MISMATCH,
            )
        object.__setattr__(self, "adds", adds)
        object.__setattr__(self, "drops", drops)


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    event_id: str
    kind: ReplayEventKind
    at: datetime
    game_ids: tuple[str, ...] = ()
    transaction: ReplayTransaction | None = None

    def __post_init__(self) -> None:
        _require_text(self.event_id, "Replay event ID")
        _require_aware(self.at, "Replay event time")
        game_ids = _normalize_ids(self.game_ids)
        object.__setattr__(self, "game_ids", game_ids)
        if self.kind is ReplayEventKind.TRANSACTION_EFFECT:
            if self.transaction is None:
                raise ReplayRunnerError("Transaction events require a transaction")
            if self.transaction.effective_at != self.at:
                raise ReplayRunnerError("Transaction event time differs from its transaction")
        elif self.transaction is not None:
            raise ReplayRunnerError("Only transaction events may contain transaction data")
        if self.kind is ReplayEventKind.TIPOFF_BATCH and not self.game_ids:
            raise ReplayRunnerError("Tipoff batches require at least one game")
        if self.kind is ReplayEventKind.GAME_FINALIZATION and len(self.game_ids) != 1:
            raise ReplayRunnerError("Game finalization events require exactly one game")


@dataclass(frozen=True, slots=True)
class ReplayPlanningSnapshot:
    event_id: str
    decision_time: datetime
    state: TeamWeekState

    def __post_init__(self) -> None:
        _require_text(self.event_id, "Replay snapshot event ID")
        _require_aware(self.decision_time, "Replay snapshot decision time")
        if self.state.decision_time != self.decision_time:
            raise ReplayRunnerError("Replay snapshot time does not match its team-week state")


@dataclass(frozen=True, slots=True)
class ReplayTrace:
    events: tuple[ReplayEvent, ...]
    planning_snapshots: tuple[ReplayPlanningSnapshot, ...]

    @property
    def fingerprint(self) -> str:
        return sha256_text(canonical_json(self))

    def to_dict(self) -> dict[str, object]:
        payload = canonicalize(self)
        assert isinstance(payload, dict)
        payload["fingerprint"] = self.fingerprint
        return payload


@dataclass(frozen=True, slots=True)
class ReplayRunnerConfig:
    planning_lead_time: timedelta = timedelta(minutes=1)

    def __post_init__(self) -> None:
        if self.planning_lead_time < timedelta(0):
            raise ReplayRunnerError("Planning lead time cannot be negative")


class ChronologicalReplayRunner:
    """Advance replay evidence chronologically and build point-in-time planning states."""

    def __init__(
        self,
        replay_state: ReplayState,
        *,
        config: ReplayConfig,
        transactions: Iterable[ReplayTransaction] = (),
        planning_cutoffs: Iterable[datetime] | None = None,
        runner_config: ReplayRunnerConfig | None = None,
        initial_roster_player_ids: Iterable[str] | None = None,
        observed_starter_ids: Iterable[str | None] = (),
        league_profile: LeagueProfile | None = None,
        player_positions_by_id: Mapping[str, Iterable[str]] | None = None,
        manager_policy_version: str = "replay-policy-v1",
        input_version: str = "replay-inputs-v1",
        week_end: datetime | None = None,
    ) -> None:
        self.replay_state = replay_state
        self.config = config
        self.runner_config = runner_config or ReplayRunnerConfig()
        self.transactions = tuple(transactions)
        self.planning_cutoffs = tuple(planning_cutoffs) if planning_cutoffs is not None else None
        self.initial_roster_player_ids = (
            None if initial_roster_player_ids is None else _normalize_ids(initial_roster_player_ids)
        )
        self.observed_starter_ids = _normalize_starter_ids(observed_starter_ids)
        self.league_profile = league_profile
        self.player_positions_by_id = player_positions_by_id
        self.manager_policy_version = manager_policy_version
        self.input_version = input_version
        self.week_end = week_end
        self._events = build_chronological_events(
            replay_state,
            transactions=self.transactions,
            planning_cutoffs=self.planning_cutoffs,
            planning_lead_time=self.runner_config.planning_lead_time,
            week_end=week_end,
        )

    @property
    def events(self) -> tuple[ReplayEvent, ...]:
        return self._events

    def run(self) -> ReplayTrace:
        roster = set(self._initial_roster())
        snapshots: list[ReplayPlanningSnapshot] = []
        for event in self._events:
            if event.kind is ReplayEventKind.TRANSACTION_EFFECT:
                assert event.transaction is not None
                if event.transaction.roster_id == self.config.roster_id:
                    _apply_transaction(roster, event.transaction)
                continue
            if event.kind is not ReplayEventKind.PLANNING_CUTOFF:
                continue
            state = team_week_state_from_replay(
                self._state_visible_at(event.at, roster),
                config=self.config,
                decision_time=event.at,
                league_profile=self.league_profile,
                observed_starter_ids=self.observed_starter_ids,
                roster_player_ids=tuple(sorted(roster)),
                player_positions_by_id=self.player_positions_by_id,
                manager_policy_version=self.manager_policy_version,
                input_version=self.input_version,
            )
            snapshots.append(ReplayPlanningSnapshot(event.event_id, event.at, state))
        return ReplayTrace(self._events, tuple(snapshots))

    def _initial_roster(self) -> tuple[str, ...]:
        if self.initial_roster_player_ids is not None:
            return self.initial_roster_player_ids
        roster = {
            player_game.sleeper_id
            for player_game in self.replay_state.player_games
            if player_game.fantasy_team_id == self.config.roster_id
            and player_game.rostered_at_tipoff
        }
        relevant_transactions = tuple(
            transaction
            for transaction in self.transactions
            if transaction.roster_id == self.config.roster_id
        )
        for transaction in sorted(relevant_transactions, key=_transaction_sort_key, reverse=True):
            roster.difference_update(transaction.adds)
            roster.update(transaction.drops)
        return tuple(sorted(roster))

    def _state_visible_at(self, at: datetime, roster: set[str]) -> ReplayState:
        games = {game.game_id: game for game in self.replay_state.games}
        player_games = tuple(
            _player_game_visible_at(player_game, games, at, roster)
            for player_game in self.replay_state.player_games
        )
        return replace(
            self.replay_state,
            player_games=player_games,
            locked_slots=tuple(
                locked for locked in self.replay_state.locked_slots if locked.locked_at <= at
            ),
            decisions=tuple(
                decision for decision in self.replay_state.decisions if decision.decision_time <= at
            ),
        )


def run_chronological_replay(
    replay_state: ReplayState,
    *,
    config: ReplayConfig,
    transactions: Iterable[ReplayTransaction] = (),
    planning_cutoffs: Iterable[datetime] | None = None,
    runner_config: ReplayRunnerConfig | None = None,
    initial_roster_player_ids: Iterable[str] | None = None,
    observed_starter_ids: Iterable[str | None] = (),
    league_profile: LeagueProfile | None = None,
    player_positions_by_id: Mapping[str, Iterable[str]] | None = None,
    manager_policy_version: str = "replay-policy-v1",
    input_version: str = "replay-inputs-v1",
    week_end: datetime | None = None,
) -> ReplayTrace:
    return ChronologicalReplayRunner(
        replay_state,
        config=config,
        transactions=transactions,
        planning_cutoffs=planning_cutoffs,
        runner_config=runner_config,
        initial_roster_player_ids=initial_roster_player_ids,
        observed_starter_ids=observed_starter_ids,
        league_profile=league_profile,
        player_positions_by_id=player_positions_by_id,
        manager_policy_version=manager_policy_version,
        input_version=input_version,
        week_end=week_end,
    ).run()


def build_chronological_events(
    replay_state: ReplayState,
    *,
    transactions: Iterable[ReplayTransaction] = (),
    planning_cutoffs: Iterable[datetime] | None = None,
    planning_lead_time: timedelta = timedelta(minutes=1),
    week_end: datetime | None = None,
) -> tuple[ReplayEvent, ...]:
    if planning_lead_time < timedelta(0):
        raise ReplayRunnerError("Planning lead time cannot be negative")
    games = _index_games(replay_state.games)
    transaction_records = tuple(transactions)
    _validate_transactions(transaction_records)
    events: list[ReplayEvent] = [
        ReplayEvent(
            event_id=f"transaction:{transaction.transaction_id}",
            kind=ReplayEventKind.TRANSACTION_EFFECT,
            at=transaction.effective_at,
            transaction=transaction,
        )
        for transaction in transaction_records
    ]

    tipoffs: dict[datetime, list[str]] = defaultdict(list)
    for game in games.values():
        _validate_game(game)
        if game.status in (ReplayGameStatus.SCHEDULED, ReplayGameStatus.FINAL):
            tipoffs[game.start_time].append(game.game_id)
        finalized_at = game.finalized_at
        if finalized_at is not None:
            events.append(
                ReplayEvent(
                    event_id=f"finalization:{game.game_id}",
                    kind=ReplayEventKind.GAME_FINALIZATION,
                    at=finalized_at,
                    game_ids=(game.game_id,),
                )
            )
    for at, game_ids in tipoffs.items():
        ordered_game_ids = tuple(sorted(game_ids))
        events.append(
            ReplayEvent(
                event_id=f"tipoff:{at.isoformat()}:{','.join(ordered_game_ids)}",
                kind=ReplayEventKind.TIPOFF_BATCH,
                at=at,
                game_ids=ordered_game_ids,
            )
        )

    cutoff_values = (
        tuple(planning_cutoffs)
        if planning_cutoffs is not None
        else tuple(at - planning_lead_time for at in sorted(tipoffs))
    )
    seen_cutoffs: set[datetime] = set()
    for at in cutoff_values:
        _require_aware(at, "Planning cutoff")
        if at in seen_cutoffs:
            continue
        seen_cutoffs.add(at)
        events.append(
            ReplayEvent(
                event_id=f"planning:{at.isoformat()}",
                kind=ReplayEventKind.PLANNING_CUTOFF,
                at=at,
            )
        )

    event_times = [event.at for event in events]
    if not event_times:
        raise ReplayRunnerError("Replay requires at least one game, transaction, or cutoff")
    resolved_week_end = week_end or max(event_times) + timedelta(microseconds=1)
    _require_aware(resolved_week_end, "Replay week end")
    if resolved_week_end < max(event_times):
        raise ReplayRunnerError("Replay week end precedes a replay event")
    events.append(
        ReplayEvent(
            event_id="week-end",
            kind=ReplayEventKind.WEEK_END,
            at=resolved_week_end,
        )
    )
    _validate_unique_event_ids(events)
    return tuple(sorted(events, key=_event_sort_key))


def _index_games(games: Iterable[ReplayGame]) -> dict[str, ReplayGame]:
    indexed: dict[str, ReplayGame] = {}
    for game in games:
        if game.game_id in indexed:
            raise ReplayRunnerError(
                f"Replay contains duplicate game ID {game.game_id!r}",
                reason=PlanningReasonCode.AMBIGUOUS_EVENT_ORDER,
            )
        indexed[game.game_id] = game
    return indexed


def _validate_game(game: ReplayGame) -> None:
    _require_aware(game.start_time, "Replay game start")
    if game.final_time is not None:
        _require_aware(game.final_time, "Replay game finalization")
        if game.final_time < game.start_time:
            raise ReplayRunnerError("Replay game finalization precedes tipoff")


def _validate_transactions(transactions: tuple[ReplayTransaction, ...]) -> None:
    ids = tuple(transaction.transaction_id for transaction in transactions)
    if len(set(ids)) != len(ids):
        raise ReplayRunnerError(
            "Replay transactions require unique IDs",
            reason=PlanningReasonCode.AMBIGUOUS_EVENT_ORDER,
        )
    by_time: dict[datetime, list[ReplayTransaction]] = defaultdict(list)
    for transaction in transactions:
        by_time[transaction.effective_at].append(transaction)
    for same_time in by_time.values():
        for index, first in enumerate(same_time):
            first_players = set(first.adds) | set(first.drops)
            for second in same_time[index + 1 :]:
                if first.roster_id != second.roster_id:
                    continue
                if first_players & (set(second.adds) | set(second.drops)):
                    raise ReplayRunnerError(
                        "Replay transactions affecting the same player at the same time "
                        "have ambiguous ordering",
                        reason=PlanningReasonCode.AMBIGUOUS_EVENT_ORDER,
                    )


def _apply_transaction(roster: set[str], transaction: ReplayTransaction) -> None:
    missing_drops = set(transaction.drops) - roster
    duplicate_adds = set(transaction.adds) & roster
    if missing_drops or duplicate_adds:
        details = []
        if missing_drops:
            details.append(f"missing drops={','.join(sorted(missing_drops))}")
        if duplicate_adds:
            details.append(f"duplicate adds={','.join(sorted(duplicate_adds))}")
        raise ReplayRunnerError(
            f"Replay transaction {transaction.transaction_id!r} cannot be applied: "
            + "; ".join(details),
            reason=PlanningReasonCode.ROSTER_STATE_MISMATCH,
        )
    roster.difference_update(transaction.drops)
    roster.update(transaction.adds)


def _player_game_visible_at(
    player_game: ReplayPlayerGame,
    games: Mapping[str, ReplayGame],
    at: datetime,
    roster: set[str],
) -> ReplayPlayerGame:
    game = games.get(player_game.game_id)
    if game is None or game.start_time < at:
        return player_game
    return replace(
        player_game,
        rostered_at_tipoff=player_game.sleeper_id in roster,
        membership_segment=None,
    )


def _event_sort_key(event: ReplayEvent) -> tuple[datetime, int, str]:
    precedence = {
        ReplayEventKind.TRANSACTION_EFFECT: 10,
        ReplayEventKind.PLANNING_CUTOFF: 20,
        ReplayEventKind.TIPOFF_BATCH: 30,
        ReplayEventKind.GAME_FINALIZATION: 40,
        ReplayEventKind.WEEK_END: 50,
    }[event.kind]
    return event.at, precedence, event.event_id


def _transaction_sort_key(transaction: ReplayTransaction) -> tuple[datetime, str]:
    return transaction.effective_at, transaction.transaction_id


def _validate_unique_event_ids(events: Iterable[ReplayEvent]) -> None:
    ids = tuple(event.event_id for event in events)
    if len(set(ids)) != len(ids):
        raise ReplayRunnerError(
            "Replay events require unique IDs",
            reason=PlanningReasonCode.AMBIGUOUS_EVENT_ORDER,
        )


def _normalize_ids(values: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
    return tuple(sorted(normalized))


def _normalize_starter_ids(values: Iterable[str | None]) -> tuple[str | None, ...]:
    result: list[str | None] = []
    for value in values:
        if value is None:
            result.append(None)
            continue
        normalized = value.strip()
        result.append(normalized if normalized and normalized != "0" else None)
    return tuple(result)


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None:
        raise ReplayRunnerError(f"{label} must be timezone-aware")


def _require_text(value: str, label: str) -> None:
    if not value.strip():
        raise ReplayRunnerError(f"{label} must be non-empty")


__all__ = (
    "ChronologicalReplayRunner",
    "ReplayEvent",
    "ReplayEventKind",
    "ReplayPlanningSnapshot",
    "ReplayRunnerConfig",
    "ReplayRunnerError",
    "ReplayTrace",
    "ReplayTransaction",
    "build_chronological_events",
    "run_chronological_replay",
)
