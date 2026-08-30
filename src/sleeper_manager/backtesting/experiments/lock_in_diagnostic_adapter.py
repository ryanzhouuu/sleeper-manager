"""Chronologically adapt the Lock-In policy to historical replay events."""

from __future__ import annotations

from datetime import datetime

from sleeper_manager.backtesting.experiments.lock_in_diagnostic_models import (
    CandidateBatch,
    DiagnosticDeferral,
    LockInDiagnosticError,
    LockInDiagnosticRequest,
)
from sleeper_manager.backtesting.replay.engine import ReplayConfig
from sleeper_manager.backtesting.replay.inputs.models import HistoricalTeamWeekInput
from sleeper_manager.backtesting.replay.models import LockCandidate, ReplayPlayerGame
from sleeper_manager.backtesting.replay.planning_adapter import team_week_state_from_replay
from sleeper_manager.backtesting.replay.runner import (
    ReplayEvent,
    ReplayEventKind,
    build_chronological_events,
)
from sleeper_manager.backtesting.replay.state import ReplayState
from sleeper_manager.decisions.lock_in import (
    ScoreMaximizingLockInPolicy,
    decision_critical_opportunities,
    missing_decision_projection_keys,
)
from sleeper_manager.domain.lock_in import LockInDecisionTrace
from sleeper_manager.domain.planning import GameOpportunity, TeamWeekState


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
        self.policy_traces: list[LockInDecisionTrace] = []
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
                planning_state = self._planning_state(event.at)
                completed_opportunity = _completed_opportunity(planning_state, player_game)
                remaining = decision_critical_opportunities(
                    planning_state,
                    completed_opportunity,
                )
                missing = missing_decision_projection_keys((completed_opportunity, *remaining))
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
                decision = self.policy.decide_after_game(planning_state, completed_opportunity)
                self.state = self.state.apply_decision(candidate, decision)
                self.decided.add(candidate_id)
                self.policy_traces.append(
                    LockInDecisionTrace(
                        decision=decision,
                        event_id=event.event_id,
                        batch_id=batch_id,
                        candidate_id=candidate_id,
                        evaluation_order=self._evaluation_counter,
                    )
                )

    def _planning_state(self, decision_time: datetime) -> TeamWeekState:
        """Adapt current replay transitions into the shared policy boundary."""

        team_week = self.request.team_week
        return team_week_state_from_replay(
            self.state,
            config=replay_config(team_week),
            decision_time=decision_time,
            observed_starter_ids=team_week.observed_starter_ids,
            roster_player_ids=team_week.roster_player_ids,
            manager_policy_version=self.request.policy_name,
            input_version=f"{team_week.manifest_id}:{team_week.league_id}:{team_week.week}",
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


def _completed_opportunity(
    state: TeamWeekState,
    player_game: ReplayPlayerGame,
) -> GameOpportunity:
    """Resolve the shared finalized opportunity for one replay candidate."""

    opportunity = next(
        (
            item
            for item in state.opportunities
            if item.sleeper_player_id == player_game.sleeper_id
            and item.game_id == player_game.game_id
        ),
        None,
    )
    if opportunity is None:
        raise LockInDiagnosticError("Replay candidate is absent from shared planning state")
    return opportunity


__all__ = ("DiagnosticPolicyAdapter", "replay_config")
