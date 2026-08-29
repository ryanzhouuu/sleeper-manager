"""Chronologically adapt the Lock-In policy to historical replay events."""

from __future__ import annotations

from datetime import datetime

from sleeper_manager.backtesting.experiments.lock_in_diagnostic_models import (
    CandidateBatch,
    DiagnosticDeferral,
    DiagnosticPolicyTrace,
    LockInDiagnosticError,
    LockInDiagnosticRequest,
)
from sleeper_manager.backtesting.replay.engine import ReplayConfig
from sleeper_manager.backtesting.replay.inputs.models import HistoricalTeamWeekInput
from sleeper_manager.backtesting.replay.models import LockCandidate, ReplayPlayerGame
from sleeper_manager.backtesting.replay.runner import (
    ReplayEvent,
    ReplayEventKind,
    build_chronological_events,
)
from sleeper_manager.backtesting.replay.state import ReplayState
from sleeper_manager.decisions.lock_in import ScoreMaximizingLockInPolicy
from sleeper_manager.domain.eligibility import eligible_for_slot


class DiagnosticPolicyAdapter:
    """Replay chronological cutoffs while recording deterministic policy evidence."""

    def __init__(self, request: LockInDiagnosticRequest) -> None:
        """Initialize replay state and trace collectors for one admitted request."""

        self.request = request
        self.state = ReplayState(
            starter_slots=request.team_week.starter_slots,
            games=request.team_week.games,
            player_games=request.team_week.player_games,
        )
        self.policy = ScoreMaximizingLockInPolicy(request.policy_config)
        self.decided: set[str] = set()
        self.deferred_seen: set[str] = set()
        self.policy_traces: list[DiagnosticPolicyTrace] = []
        self.deferrals: list[DiagnosticDeferral] = []
        self.batches: list[CandidateBatch] = []
        self.evaluation_order: list[str] = []
        self._batch_ids: set[str] = set()
        self._evaluation_counter = 0

    def run(self) -> None:
        """Evaluate planning cutoffs and expire unresolved deferrals at week end."""

        events = build_chronological_events(
            self.state,
            planning_cutoffs=self.request.planning_cutoffs,
            planning_lead_time=self.request.planning_lead_time,
        )
        for event in events:
            if event.kind is ReplayEventKind.PLANNING_CUTOFF:
                self._evaluate_cutoff(event)
            elif event.kind is ReplayEventKind.WEEK_END:
                self._expire_deferred(event)

    def _evaluate_cutoff(self, event: ReplayEvent) -> None:
        """Evaluate every newly final candidate at one chronological cutoff."""

        candidates = self._open_candidates(event.at)
        if not candidates:
            return
        for batch_id, finalized_at, batch_candidates in self._group_batches(candidates):
            if batch_id not in self._batch_ids:
                self._batch_ids.add(batch_id)
                self.batches.append(
                    CandidateBatch(
                        batch_id=batch_id,
                        finalized_at=finalized_at,
                        candidate_ids=tuple(
                            _candidate_id(player_game) for player_game, _ in batch_candidates
                        ),
                    )
                )
            for player_game, candidate in batch_candidates:
                candidate_id = _candidate_id(player_game)
                if candidate_id in self.decided:
                    continue
                self._evaluation_counter += 1
                self.evaluation_order.append(candidate_id)
                remaining = _decision_critical_remaining(
                    self.state,
                    completed=player_game,
                    decision_time=event.at,
                )
                missing = _missing_projection_keys(
                    (player_game, *remaining),
                    decision_time=event.at,
                )
                if missing:
                    self.deferred_seen.add(candidate_id)
                    self.deferrals.append(
                        DiagnosticDeferral(
                            candidate_id=candidate_id,
                            sleeper_id=player_game.sleeper_id,
                            game_id=player_game.game_id,
                            cutoff=event.at,
                            event_id=event.event_id,
                            batch_id=batch_id,
                            missing_opportunity_keys=missing,
                            reason=(
                                "Deferred because at least one decision-critical future "
                                "opportunity lacked an as-of projection."
                            ),
                        )
                    )
                    continue
                decision = self.policy.decide_after_game(
                    player_game,
                    remaining_games=remaining,
                    open_slots=tuple(
                        (index, self.state.starter_slots[index])
                        for index in self.state.open_slot_indices
                    ),
                    locked_slots=self.state.locked_slots,
                    decision_time=event.at,
                    league_id=self.request.team_week.league_id,
                    week=self.request.team_week.week,
                    roster_id=self.request.team_week.roster_id,
                    run_seed=self.request.policy_config.seed,
                    scenario_count=self.request.policy_config.scenario_count,
                )
                if decision.kind == "lock":
                    if decision.slot_index is None:
                        raise LockInDiagnosticError("Lock decision is missing a slot index")
                    self.state = self.state.lock(
                        candidate,
                        slot_index=decision.slot_index,
                        at=event.at,
                        information_version=decision.information_version,
                        reason=decision.reason,
                    )
                elif decision.kind == "pass":
                    self.state = self.state.pass_candidate(
                        candidate,
                        at=event.at,
                        information_version=decision.information_version,
                        reason=decision.reason,
                    )
                else:
                    raise LockInDiagnosticError(
                        f"Unsupported policy decision kind {decision.kind!r}"
                    )
                self.decided.add(candidate_id)
                self.policy_traces.append(
                    DiagnosticPolicyTrace(
                        decision=decision,
                        decision_time=event.at,
                        event_id=event.event_id,
                        batch_id=batch_id,
                        candidate_id=candidate_id,
                        evaluation_order=self._evaluation_counter,
                    )
                )

    def _expire_deferred(self, event: ReplayEvent) -> None:
        """Record terminal evidence for candidates still deferred at week end."""

        for candidate_id in sorted(self.deferred_seen - self.decided):
            sleeper_id, game_id = candidate_id.split(":", 1)
            self.deferrals.append(
                DiagnosticDeferral(
                    candidate_id=candidate_id,
                    sleeper_id=sleeper_id,
                    game_id=game_id,
                    cutoff=event.at,
                    event_id=event.event_id,
                    batch_id="expired",
                    missing_opportunity_keys=(),
                    reason="Candidate expired after one or more projection deferrals.",
                    terminal=True,
                )
            )

    def _open_candidates(self, at: datetime) -> list[tuple[ReplayPlayerGame, LockCandidate]]:
        """Return undecided candidates whose outcomes are visible at the cutoff."""

        found: list[tuple[ReplayPlayerGame, LockCandidate]] = []
        for player_game in self.state.player_games:
            candidate_id = _candidate_id(player_game)
            if candidate_id in self.decided:
                continue
            candidate = self.state.candidate_at(player_game, at)
            if candidate is None:
                continue
            found.append((player_game, candidate))
        return found

    def _group_batches(
        self, candidates: list[tuple[ReplayPlayerGame, LockCandidate]]
    ) -> list[tuple[str, datetime, list[tuple[ReplayPlayerGame, LockCandidate]]]]:
        """Group visible candidates by finalization time with stable ordering."""

        game_by_id = {game.game_id: game for game in self.state.games}
        by_finalization: dict[datetime, list[tuple[ReplayPlayerGame, LockCandidate]]] = {}
        for player_game, candidate in candidates:
            game = game_by_id[player_game.game_id]
            finalized_at = game.finalized_at
            if finalized_at is None:
                raise LockInDiagnosticError(
                    f"Finalized candidate {player_game.game_id!r} is missing final_time"
                )
            by_finalization.setdefault(finalized_at, []).append((player_game, candidate))
        batches: list[tuple[str, datetime, list[tuple[ReplayPlayerGame, LockCandidate]]]] = []
        for finalized_at in sorted(by_finalization):
            ordered = sorted(
                by_finalization[finalized_at],
                key=lambda item: (
                    item[0].sleeper_id,
                    game_by_id[item[0].game_id].start_time,
                    item[0].game_id,
                ),
            )
            batch_id = f"batch:{finalized_at.isoformat()}"
            batches.append((batch_id, finalized_at, ordered))
        return batches


def replay_config(team_week: HistoricalTeamWeekInput) -> ReplayConfig:
    """Build the replay identity and slot configuration for one team-week."""

    return ReplayConfig(
        starter_slots=team_week.starter_slots,
        league_id=team_week.league_id,
        week=team_week.week,
        roster_id=team_week.roster_id,
        eligibility_quality=team_week.eligibility_quality.value,
    )


def _candidate_id(player_game: ReplayPlayerGame) -> str:
    """Return the stable player-game identity used by diagnostic traces."""

    return f"{player_game.sleeper_id}:{player_game.game_id}"


def _opportunity_key(player_game: ReplayPlayerGame) -> str:
    """Return the stable key used to report missing projection opportunities."""

    return f"{player_game.sleeper_id}:{player_game.game_id}"


def _decision_critical_remaining(
    state: ReplayState,
    *,
    completed: ReplayPlayerGame,
    decision_time: datetime,
) -> tuple[ReplayPlayerGame, ...]:
    """Select future rostered opportunities that can still fill an open slot."""

    locked_players = {slot.sleeper_id for slot in state.locked_slots}
    open_slots = tuple((index, state.starter_slots[index]) for index in state.open_slot_indices)
    game_by_id = {game.game_id: game for game in state.games}
    remaining: list[ReplayPlayerGame] = []
    for player_game in state.player_games:
        if player_game.sleeper_id in locked_players:
            continue
        if (
            player_game.sleeper_id == completed.sleeper_id
            and player_game.game_id == completed.game_id
        ):
            continue
        if not player_game.rostered_at_tipoff:
            continue
        game = game_by_id.get(player_game.game_id)
        if game is None or game.start_time <= decision_time:
            continue
        if not any(
            eligible_for_slot(player_game.eligible_positions, position)
            for _, position in open_slots
        ):
            continue
        remaining.append(player_game)
    return tuple(
        sorted(
            remaining,
            key=lambda item: (
                item.sleeper_id,
                game_by_id[item.game_id].start_time,
                item.game_id,
            ),
        )
    )


def _missing_projection_keys(
    player_games: tuple[ReplayPlayerGame, ...],
    *,
    decision_time: datetime,
) -> tuple[str, ...]:
    """Identify candidate projections unavailable at the decision cutoff."""

    missing: list[str] = []
    for player_game in player_games:
        projection = player_game.projection
        if projection is None or projection.available_as_of > decision_time:
            missing.append(_opportunity_key(player_game))
    return tuple(missing)


__all__ = ("DiagnosticPolicyAdapter", "replay_config")
