"""Execute counterfactual weekly lineups and Lock-In decisions chronologically."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from sleeper_manager.backtesting.experiments.full_advisor_replay_legality import (
    active_target_slots,
    automatic_final_scores,
    realign_for_lock,
)
from sleeper_manager.backtesting.experiments.full_advisor_replay_models import (
    FULL_ADVISOR_EXECUTOR_VERSION,
    FullAdvisorReplayError,
    FullAdvisorReplayExecution,
    FullAdvisorReplayRequest,
    ReplayLineupTrace,
    StartedPlayerGame,
)
from sleeper_manager.backtesting.experiments.lock_in_diagnostic import (
    admit_historical_team_week,
)
from sleeper_manager.backtesting.experiments.lock_in_diagnostic_oracle import (
    oracle_feasibility_checks,
    oracle_from_planning_state,
)
from sleeper_manager.backtesting.replay.engine import ReplayConfig, compare_team_week
from sleeper_manager.backtesting.replay.models import ReplayPlayerGame, TeamWeekReplayResult
from sleeper_manager.backtesting.replay.planning_adapter import team_week_state_from_replay
from sleeper_manager.backtesting.replay.projection_surface import (
    HistoricalProjectionSurfaceError,
    full_advisor_events,
    full_advisor_planning_cutoffs,
    player_games_with_projection_surface,
    validate_historical_projection_surface,
)
from sleeper_manager.backtesting.replay.runner import (
    ReplayEvent,
    ReplayEventKind,
)
from sleeper_manager.backtesting.replay.state import ReplayState
from sleeper_manager.decisions.live_lock_in import (
    LiveLockInPolicyConfig,
    evaluate_live_lock_in,
)
from sleeper_manager.decisions.lock_in import ScoreMaximizingLockInPolicy
from sleeper_manager.decisions.weekly_plan import build_weekly_plan
from sleeper_manager.domain.lock_in import LockInEvaluation, LockInEvaluationKind
from sleeper_manager.domain.planning import (
    GameOpportunity,
    PlanStatus,
    TeamWeekState,
    WeeklyPlan,
)


class _FullAdvisorExecutor:
    """Own mutable chronological state for one bounded replay attempt."""

    def __init__(self, request: FullAdvisorReplayRequest) -> None:
        """Initialize replay state without consuming historical starter choices."""

        self.request = request
        self.team_week = request.team_week
        self.state = ReplayState(
            starter_slots=self.team_week.starter_slots,
            games=self.team_week.games,
            player_games=self.team_week.player_games,
        )
        self.lineup: dict[int, str] = {}
        self.active: dict[str, int] = {}
        self.started: dict[tuple[str, str], StartedPlayerGame] = {}
        self.pending: set[tuple[str, str]] = set()
        self.terminal: set[tuple[str, str]] = set()
        self.plans: list[WeeklyPlan] = []
        self.evaluations: list[LockInEvaluation] = []
        self.lineup_traces: list[ReplayLineupTrace] = []
        self.policy = ScoreMaximizingLockInPolicy(request.lock_in_policy_config)
        try:
            validate_historical_projection_surface(
                request.projection_surface,
                team_week=self.team_week,
                planning_lead_time=request.planning_lead_time,
            )
        except HistoricalProjectionSurfaceError as error:
            raise FullAdvisorReplayError(f"projection_surface_invalid:{error}") from error
        self.events = full_advisor_events(self.team_week, request.planning_lead_time)
        self.planning_cutoffs = frozenset(
            full_advisor_planning_cutoffs(self.team_week, request.planning_lead_time)
        )
        self.week_end = next(
            event.at for event in self.events if event.kind is ReplayEventKind.WEEK_END
        )

    def run(self) -> FullAdvisorReplayExecution:
        """Advance every event and return a legal model/oracle comparison."""

        for event in self.events:
            if event.kind is ReplayEventKind.PLANNING_CUTOFF:
                self._evaluate_pending(event)
                self._plan_lineup(event)
            elif event.kind is ReplayEventKind.TIPOFF_BATCH:
                self._expire_pending(event)
                self._record_tipoff(event)
            elif event.kind is ReplayEventKind.GAME_FINALIZATION:
                self._record_finalization(event)
            elif event.kind is ReplayEventKind.WEEK_END:
                self._expire_pending(event)
                return self._finish(event)
        raise FullAdvisorReplayError("week_end event was not executed")

    def _plan_lineup(self, event: ReplayEvent) -> None:
        """Run the shared weekly planner and apply its desired assignments."""

        state = self._planning_state(event.at)
        plan = build_weekly_plan(
            state,
            lead_time=self.request.planning_lead_time,
            policy=self.request.weekly_policy_config,
        )
        self.plans.append(plan)
        if plan.status is PlanStatus.BLOCKED:
            reasons = ",".join(reason.value for reason in plan.blocking_reasons)
            raise FullAdvisorReplayError(f"planning_blocked:{reasons}")
        desired = {
            assignment.slot_index: assignment.player_id
            for assignment in plan.desired_assignments
            if assignment.player_id is not None
        }
        self.active = active_target_slots(
            desired=desired,
            active=self.active,
            planning_state=state,
            event_id=event.event_id,
        )
        self.lineup = desired
        self._trace_lineup(event)

    def _record_tipoff(self, event: ReplayEvent) -> None:
        """Freeze simulated starter evidence for every player in the tipoff batch."""

        game_ids = set(event.game_ids)
        locked_players = {slot.player_id for slot in self.state.locked_slots}
        for slot_index, player_id in sorted(self.lineup.items()):
            if player_id in locked_players:
                continue
            for player_game in self.state.player_games:
                if player_game.sleeper_id != player_id or player_game.game_id not in game_ids:
                    continue
                if not player_game.rostered_at_tipoff:
                    raise FullAdvisorReplayError(
                        f"unrostered_start:{player_id}:{player_game.game_id}"
                    )
                key = (player_id, player_game.game_id)
                started = StartedPlayerGame(player_id, player_game.game_id, slot_index, event.at)
                self.started[key] = started
                self.active[player_id] = slot_index
        self._trace_lineup(event)

    def _record_finalization(self, event: ReplayEvent) -> None:
        """Expose final outcomes and evaluate newly actionable simulated starters."""

        game_id = event.game_ids[0]
        keys = sorted(key for key in self.started if key[1] == game_id)
        for player_id, _ in keys:
            self.active.pop(player_id, None)
        for key in keys:
            self.pending.add(key)
            self._evaluate_key(key, event)

    def _evaluate_pending(self, event: ReplayEvent) -> None:
        """Re-evaluate prior Wait outcomes before the next lineup decision."""

        for key in sorted(self.pending):
            self._evaluate_key(key, event)

    def _expire_pending(self, event: ReplayEvent) -> None:
        """Record unresolved Wait outcomes when their action deadline arrives."""

        for key in sorted(self.pending):
            if self._deadline(self._player_game(key)) <= event.at:
                self._evaluate_key(key, event)

    def _evaluate_key(self, key: tuple[str, str], event: ReplayEvent) -> None:
        """Apply one current live evaluation when it becomes actionable."""

        if key in self.terminal:
            self.pending.discard(key)
            return
        player_game = self._player_game(key)
        state = self._planning_state(event.at)
        opportunity = self._opportunity(state, key)
        deadline = self._deadline(player_game)
        evaluation = evaluate_live_lock_in(
            state,
            opportunity,
            deadline=deadline,
            manager_policy_version=self.request.policy_name,
            policy=self.policy,
            config=LiveLockInPolicyConfig(self.request.minimum_confidence),
        )
        self.evaluations.append(evaluation)
        if evaluation.kind is LockInEvaluationKind.WAIT:
            return
        if evaluation.kind is LockInEvaluationKind.UNAVAILABLE:
            if "deadline_elapsed" in evaluation.reason_codes:
                self.pending.discard(key)
                self.terminal.add(key)
                return
            reasons = ",".join(evaluation.reason_codes)
            raise FullAdvisorReplayError(f"lock_in_unavailable:{key[0]}:{key[1]}:{reasons}")
        self.pending.discard(key)
        self.terminal.add(key)
        decision = evaluation.decision
        if decision is None:
            return
        candidate = self.state.candidate_at(player_game, event.at)
        if candidate is None:
            raise FullAdvisorReplayError(f"illegal_candidate:{key[0]}:{key[1]}")
        if evaluation.kind is LockInEvaluationKind.LOCK:
            assert decision.slot_index is not None
            self.lineup = realign_for_lock(
                player_id=key[0],
                target_slot=decision.slot_index,
                lineup=self.lineup,
                active=self.active,
                replay_state=self.state,
                team_week=self.team_week,
            )
        self.state = self.state.apply_decision(candidate, decision)
        self._trace_lineup(event)

    def _finish(self, event: ReplayEvent) -> FullAdvisorReplayExecution:
        """Assign legal automatic scores, build the oracle, and verify invariants."""

        if self.pending:
            unresolved = ",".join(f"{player}:{game}" for player, game in sorted(self.pending))
            raise FullAdvisorReplayError(f"unresolved_wait:{unresolved}")
        automatic = automatic_final_scores(
            team_week=self.team_week,
            replay_state=self.state,
            started_keys=frozenset(self.started),
        )
        model = TeamWeekReplayResult(
            league_id=self.team_week.league_id,
            week=self.team_week.week,
            roster_id=self.team_week.roster_id,
            policy_name=self.request.policy_name,
            realized_score=round(
                sum(slot.accepted_fantasy_score for slot in self.state.locked_slots)
                + sum(score for _, score in automatic),
                6,
            ),
            decisions=self.state.decisions,
            locked_slots=self.state.locked_slots,
            automatic_final_scores=automatic,
            eligibility_quality=self.team_week.eligibility_quality.value,
            data_quality="complete" if self.team_week.complete else "partial",
            exclusions=tuple(item.reason.value for item in self.team_week.exclusions),
        )
        oracle_state = self._planning_state(
            event.at,
            observed=False,
            replay_state=ReplayState(
                self.team_week.starter_slots,
                self.team_week.games,
                self.team_week.player_games,
            ),
        )
        oracle = oracle_from_planning_state(oracle_state)
        feasibility = oracle_feasibility_checks(oracle, oracle_state)
        if any(not check.feasible for check in feasibility):
            raise FullAdvisorReplayError("oracle_deadline_infeasible")
        comparison = compare_team_week(oracle, model)
        invariants = (
            ("model_not_above_oracle", model.realized_score <= oracle.realized_score + 1e-6),
            ("no_active_players_at_week_end", not self.active),
            (
                "all_started_games_rostered",
                all(
                    self._player_game((item.player_id, item.game_id)).rostered_at_tipoff
                    for item in self.started.values()
                ),
            ),
        )
        return FullAdvisorReplayExecution(
            status="success",
            executor_version=FULL_ADVISOR_EXECUTOR_VERSION,
            model_result=model,
            oracle_result=oracle,
            comparison=comparison,
            plans=tuple(self.plans),
            evaluations=tuple(self.evaluations),
            lineup_traces=tuple(self.lineup_traces),
            started_player_games=tuple(self.started[key] for key in sorted(self.started)),
            invariant_results=invariants,
        )

    def _planning_state(
        self,
        at: datetime,
        *,
        observed: bool = True,
        replay_state: ReplayState | None = None,
    ) -> TeamWeekState:
        """Adapt current transitions and simulated assignments at one decision time."""

        source_state = replay_state or self.state
        try:
            projected_games = player_games_with_projection_surface(
                source_state.player_games,
                self.request.projection_surface,
                game_starts={game.game_id: game.start_time for game in source_state.games},
                decision_time=at,
                exact_cutoff=at in self.planning_cutoffs,
            )
        except HistoricalProjectionSurfaceError as error:
            raise FullAdvisorReplayError(str(error)) from error
        projected_state = replace(source_state, player_games=projected_games)
        return team_week_state_from_replay(
            projected_state,
            config=ReplayConfig(
                self.team_week.starter_slots,
                self.team_week.league_id,
                self.team_week.week,
                self.team_week.roster_id,
                self.team_week.eligibility_quality.value,
            ),
            decision_time=at,
            observed_starter_ids=(
                tuple(self.lineup.get(index) for index in range(len(self.team_week.starter_slots)))
                if observed
                else ()
            ),
            roster_player_ids=self.team_week.roster_player_ids,
            manager_policy_version=self.request.policy_name,
            input_version=(
                f"{self.team_week.manifest_id}:"
                f"{self.request.projection_surface.fingerprint}:"
                f"{FULL_ADVISOR_EXECUTOR_VERSION}"
            ),
        )

    def _deadline(self, player_game: ReplayPlayerGame) -> datetime:
        """Return the next rostered game start or the replay week end."""

        current_start = self._game_start(player_game.game_id)
        later = tuple(
            self._game_start(item.game_id)
            for item in self.state.player_games
            if item.sleeper_id == player_game.sleeper_id
            if item.rostered_at_tipoff
            if self._game_start(item.game_id) > current_start
        )
        return min(later) if later else self.week_end

    def _player_game(self, key: tuple[str, str]) -> ReplayPlayerGame:
        """Resolve one unique player-game from its stable key."""

        return next(
            item for item in self.state.player_games if (item.sleeper_id, item.game_id) == key
        )

    def _opportunity(self, state: TeamWeekState, key: tuple[str, str]) -> GameOpportunity:
        """Resolve the planning opportunity matching one started player-game."""

        return next(
            item for item in state.opportunities if (item.sleeper_player_id, item.game_id) == key
        )

    def _game_start(self, game_id: str) -> datetime:
        """Resolve one replay game start time."""

        return next(game.start_time for game in self.state.games if game.game_id == game_id)

    def _trace_lineup(self, event: ReplayEvent) -> None:
        """Append one deterministic lineup transition trace."""

        self.lineup_traces.append(
            ReplayLineupTrace(event.event_id, event.at, tuple(sorted(self.lineup.items())))
        )


def run_full_advisor_replay(
    request: FullAdvisorReplayRequest,
) -> FullAdvisorReplayExecution:
    """Run one admitted current/current team-week through the full advisor path."""

    admission = admit_historical_team_week(request.team_week)
    if not admission.admitted:
        failed = ",".join(check.code for check in admission.checks if not check.passed)
        raise FullAdvisorReplayError(f"admission_failed:{failed}")
    return _FullAdvisorExecutor(request).run()


__all__ = (
    "FULL_ADVISOR_EXECUTOR_VERSION",
    "FullAdvisorReplayError",
    "FullAdvisorReplayExecution",
    "FullAdvisorReplayRequest",
    "ReplayLineupTrace",
    "StartedPlayerGame",
    "run_full_advisor_replay",
)
