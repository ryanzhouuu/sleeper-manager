from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from sleeper_manager.backtesting.replay.models import (
    LockCandidate,
    ReplayGame,
    ReplayPlayerGame,
)
from sleeper_manager.domain.eligibility import eligible_for_slot
from sleeper_manager.domain.lock_in import LockInDecision, LockInDecisionKind
from sleeper_manager.domain.planning import FixedSlot


class ReplayError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ReplayState:
    starter_slots: tuple[str, ...]
    games: tuple[ReplayGame, ...]
    player_games: tuple[ReplayPlayerGame, ...]
    locked_slots: tuple[FixedSlot, ...] = ()
    decisions: tuple[LockInDecision, ...] = ()

    @property
    def open_slot_indices(self) -> tuple[int, ...]:
        locked = {slot.slot_index for slot in self.locked_slots}
        return tuple(index for index in range(len(self.starter_slots)) if index not in locked)

    def candidate_at(self, player_game: ReplayPlayerGame, at: datetime) -> LockCandidate | None:
        if at.tzinfo is None:
            raise ReplayError("Replay decisions require timezone-aware timestamps")
        game = _game_for(self.games, player_game.game_id)
        if game is None or game.status.value != "final" or not player_game.rostered_at_tipoff:
            return None
        final_time = game.finalized_at
        if final_time is None or at < final_time:
            return None
        if any(slot.player_id == player_game.sleeper_id for slot in self.locked_slots):
            return None
        next_start = min(
            (
                next_game.start_time
                for next_game in self.games
                if next_game.start_time > game.start_time
                and any(
                    other.sleeper_id == player_game.sleeper_id
                    and other.game_id == next_game.game_id
                    and other.rostered_at_tipoff
                    for other in self.player_games
                )
            ),
            default=_week_end(self.games, game.start_time),
        )
        if at >= next_start:
            return None
        return LockCandidate(
            sleeper_id=player_game.sleeper_id,
            fantasy_team_id=player_game.fantasy_team_id,
            game_id=player_game.game_id,
            completed_score=player_game.actual_score,
            eligible_positions_at_tipoff=player_game.eligible_positions,
            expires_at=next_start,
        )

    def apply_decision(self, candidate: LockCandidate, decision: LockInDecision) -> ReplayState:
        """Apply one exact shared policy decision to historical replay state."""

        if decision.player_id != candidate.sleeper_id or decision.game_id != candidate.game_id:
            raise ReplayError("Lock-In decision does not identify the replay candidate")
        at = decision.decision_time
        player_game = next(
            (
                game
                for game in self.player_games
                if game.sleeper_id == candidate.sleeper_id and game.game_id == candidate.game_id
            ),
            None,
        )
        if player_game is None or self.candidate_at(player_game, at) != candidate:
            raise ReplayError("Lock-In candidate is expired or otherwise illegal")
        if decision.kind is LockInDecisionKind.PASS:
            return replace(self, decisions=self.decisions + (decision,))

        slot_index = decision.slot_index
        if slot_index is None:
            raise ReplayError("Lock decision is missing a slot index")
        if slot_index not in self.open_slot_indices:
            raise ReplayError("Lock-In slot is already locked")
        if slot_index >= len(self.starter_slots):
            raise ReplayError("Lock-In slot index is outside the roster")
        if not eligible_for_slot(
            candidate.eligible_positions_at_tipoff, self.starter_slots[slot_index]
        ):
            raise ReplayError("Player was not eligible for the selected starting slot")
        locked = FixedSlot(
            slot_index=slot_index,
            slot_position=self.starter_slots[slot_index],
            player_id=candidate.sleeper_id,
            game_id=candidate.game_id,
            accepted_fantasy_score=candidate.completed_score,
            decision_time=at,
            decision_id=_decision_id(decision),
            provenance=decision.information_version,
        )
        return replace(
            self,
            locked_slots=self.locked_slots + (locked,),
            decisions=self.decisions + (decision,),
        )

    def automatic_final_scores(self) -> tuple[tuple[str, float], ...]:
        locked_players = {slot.player_id for slot in self.locked_slots}
        scores: list[tuple[str, float]] = []
        for player_id in sorted({game.sleeper_id for game in self.player_games} - locked_players):
            player_games = tuple(game for game in self.player_games if game.sleeper_id == player_id)
            final = max(player_games, key=lambda game: _replay_game_start(self.games, game.game_id))
            scores.append((player_id, final.actual_score if final.rostered_at_tipoff else 0.0))
        return tuple(scores)


def _game_for(games: tuple[ReplayGame, ...], game_id: str) -> ReplayGame | None:
    return next((game for game in games if game.game_id == game_id), None)


def _replay_game_start(games: tuple[ReplayGame, ...], game_id: str) -> datetime:
    game = _game_for(games, game_id)
    return game.start_time if game is not None else datetime.min.replace(tzinfo=UTC)


def _week_end(games: tuple[ReplayGame, ...], current_start: datetime) -> datetime:
    return max(
        (game.start_time for game in games),
        default=current_start,
    ) + timedelta(minutes=1)


def _decision_id(decision: LockInDecision) -> str:
    """Build stable fixed-slot identity from the exact applied decision."""

    return (
        f"{decision.information_version}:{decision.player_id}:"
        f"{decision.game_id}:{decision.slot_index}:{decision.decision_time.isoformat()}"
    )


__all__ = ("ReplayError", "ReplayState")
