from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from sleeper_manager.backtesting.replay.models import (
    LockCandidate,
    LockedSlot,
    ReplayDecision,
    ReplayGame,
    ReplayPlayerGame,
)
from sleeper_manager.domain.eligibility import eligible_for_slot


class ReplayError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ReplayState:
    starter_slots: tuple[str, ...]
    games: tuple[ReplayGame, ...]
    player_games: tuple[ReplayPlayerGame, ...]
    locked_slots: tuple[LockedSlot, ...] = ()
    decisions: tuple[ReplayDecision, ...] = ()

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
        if any(slot.sleeper_id == player_game.sleeper_id for slot in self.locked_slots):
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

    def lock(
        self,
        candidate: LockCandidate,
        *,
        slot_index: int,
        at: datetime,
        information_version: str = "replay-inputs",
        reason: str = "legal Lock-In",
    ) -> ReplayState:
        if slot_index not in self.open_slot_indices:
            raise ReplayError("Lock-In slot is already locked")
        if slot_index >= len(self.starter_slots):
            raise ReplayError("Lock-In slot index is outside the roster")
        player_game = next(
            (
                game
                for game in self.player_games
                if game.sleeper_id == candidate.sleeper_id and game.game_id == candidate.game_id
            ),
            None,
        )
        if player_game is None:
            raise ReplayError("Lock candidate does not belong to this replay")
        legal_candidate = self.candidate_at(player_game, at)
        if legal_candidate != candidate:
            raise ReplayError("Lock candidate is expired or otherwise illegal")
        if not eligible_for_slot(
            candidate.eligible_positions_at_tipoff, self.starter_slots[slot_index]
        ):
            raise ReplayError("Player was not eligible for the selected starting slot")
        locked = LockedSlot(
            slot_index,
            self.starter_slots[slot_index],
            candidate.sleeper_id,
            candidate.game_id,
            candidate.completed_score,
            at,
        )
        decision = ReplayDecision(
            at,
            "lock",
            candidate.sleeper_id,
            candidate.game_id,
            slot_index,
            information_version,
            candidate.completed_score,
            candidate.completed_score,
            reason,
        )
        return replace(
            self,
            locked_slots=self.locked_slots + (locked,),
            decisions=self.decisions + (decision,),
        )

    def pass_candidate(
        self,
        candidate: LockCandidate,
        *,
        at: datetime,
        information_version: str = "replay-inputs",
        reason: str = "preserved future flexibility",
    ) -> ReplayState:
        player_game = next(
            (
                game
                for game in self.player_games
                if game.sleeper_id == candidate.sleeper_id and game.game_id == candidate.game_id
            ),
            None,
        )
        if player_game is None or self.candidate_at(player_game, at) != candidate:
            raise ReplayError("Cannot pass an expired or unknown Lock-In candidate")
        decision = ReplayDecision(
            at,
            "pass",
            candidate.sleeper_id,
            candidate.game_id,
            None,
            information_version,
            0.0,
            0.0,
            reason,
        )
        return replace(self, decisions=self.decisions + (decision,))

    def automatic_final_scores(self) -> tuple[tuple[str, float], ...]:
        locked_players = {slot.sleeper_id for slot in self.locked_slots}
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


__all__ = ("ReplayError", "ReplayState")
