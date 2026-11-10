"""Dashboard lineup rows: each player's games, their derived states, and the planner's decision.

Decisions read the latest weekly-lineup trace (schema 3). A Pass becomes "sacrifice" when the
plan benches the player and "preserve" when it keeps him in a starting slot.
"""

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from sleeper_manager.cloudflare.home_document import (
    GameRow,
    RosterRow,
    player_image_url,
    team_image_url,
)
from sleeper_manager.cloudflare.lineup_states import game_state
from sleeper_manager.persistence.base import AsyncRuntimeStateRepository
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityRecord

_WEEKLY_LINEUP = "weekly_lineup"
_NON_STARTING = frozenset({"BN", "IR"})


async def load_plan_trace(
    repository: AsyncRuntimeStateRepository, league_id: str, week: int, *, now: datetime
) -> dict[str, Any] | None:
    """The current weekly plan's trace, or None when absent, expired, or unreadable."""
    try:
        record = await repository.latest_current_recommendation(
            league_id, week, decision_type=_WEEKLY_LINEUP
        )
        if record is None or (record.deadline is not None and record.deadline <= now):
            return None
        trace = json.loads(record.trace_json)
    except Exception:
        return None
    return trace if isinstance(trace, dict) and trace.get("schema_version") == 3 else None


async def home_lineup(
    repository: AsyncRuntimeStateRepository,
    league_id: str,
    week: int,
    roster_id: int,
    rows: Sequence[RosterRow],
    games: Sequence[GameRow],
    *,
    trace: Mapping[str, Any] | None,
    now: datetime,
    sleeper_locks: Mapping[str, date] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Lineup rows and plan summary for the home document; a store failure leaves no games."""
    try:
        records = tuple(
            row
            for row in await repository.list_week_lock_in_opportunities(league_id, week)
            if row.key.roster_id == roster_id
        )
    except Exception:
        records = ()
    lineup = lineup_document(rows, games, records, trace, now=now, sleeper_locks=sleeper_locks)
    names = {row.player_id: row.name for row in rows if row.player_id and row.name}
    return lineup, plan_summary(trace, names)


def plan_summary(
    plan_trace: Mapping[str, Any] | None, names: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Team totals, the plan's 10th-90th range, and finals too close to call.

    With undecided finals the planner withholds ``team_after``; ``team_before`` is then the
    expected total if every undecided final is passed.
    """
    trace = plan_trace or {}
    undecided = trace.get("undecided_scores")
    return {
        "status": _text(trace.get("status")),
        "team_before": _number(trace.get("observed_terminal_score")),
        "team_after": _number(trace.get("expected_terminal_score")),
        "confidence": _text(trace.get("confidence")),
        "p10": _number(trace.get("terminal_score_p10")),
        "p90": _number(trace.get("terminal_score_p90")),
        "undecided": [
            _undecided(row, names or {})
            for row in (undecided if isinstance(undecided, list) else ())
            if isinstance(row, Mapping)
        ],
    }


def _undecided(row: Mapping[str, Any], names: Mapping[str, str]) -> dict[str, Any]:
    player_id = row.get("player_id")
    return {
        "name": names.get(player_id) if isinstance(player_id, str) else None,
        "pass_confidence": _number(row.get("pass_confidence")),
        "lock_confidence": _number(row.get("lock_confidence")),
    }


def lineup_document(
    rows: Sequence[RosterRow],
    games: Sequence[GameRow],
    records: Sequence[LockInOpportunityRecord],
    plan_trace: Mapping[str, Any] | None,
    *,
    now: datetime,
    sleeper_locks: Mapping[str, date] | None,
) -> list[dict[str, Any]]:
    """Rows in Sleeper slot order, then bench and reserve. ``sleeper_locks`` is a live read."""
    names = {row.player_id: row.name for row in rows if row.player_id}
    current = {row.player_id: row.slot for row in rows if row.player_id}
    decisions = _decisions(plan_trace, names, current)
    summary = plan_summary(plan_trace)
    items: list[dict[str, Any]] = []
    starting_index = 0
    for row in rows:
        slot_index = None if row.slot in _NON_STARTING else starting_index
        if slot_index is not None:
            starting_index += 1
        player_games = (
            _games(row.player_id, games, records, now, (sleeper_locks or {}).get(row.player_id))
            if row.player_id
            else []
        )
        decision = decisions.get(row.player_id or "")
        if decision is not None:
            decision = _with_opponent(decision, player_games)
            decision.update(team_before=summary["team_before"], team_after=summary["team_after"])
        items.append(
            {
                "slot": row.slot,
                "slot_index": slot_index,
                "player_id": row.player_id,
                "name": row.name,
                "nba_team": row.nba_team,
                "image_url": player_image_url(row.espn_id),
                "games": player_games,
                "decision": decision,
            }
        )
    return items


def _games(
    player_id: str,
    games: Sequence[GameRow],
    records: Sequence[LockInOpportunityRecord],
    now: datetime,
    sleeper_lock_date: date | None,
) -> list[dict[str, Any]]:
    """Schedule games joined to stored rows; a row without a schedule game still appears."""
    own = [row for row in records if row.key.player_id == player_id]
    scheduled = sorted((game for game in games if game.player_id == player_id), key=_start)
    used: set[str] = set()
    items: list[dict[str, Any]] = []
    for game in scheduled:
        match = next((row for row in own if row.scheduled_start == game.start), None)
        if match is not None:
            used.add(match.key.game_id)
        items.append(_game_item(match, game, own, now, sleeper_lock_date))
    for row in own:
        if row.key.game_id not in used:
            items.append(_game_item(row, None, own, now, sleeper_lock_date))
    return sorted(items, key=lambda item: item["start"])


def _game_item(
    record: LockInOpportunityRecord | None,
    game: GameRow | None,
    records: Sequence[LockInOpportunityRecord],
    now: datetime,
    sleeper_lock_date: date | None,
) -> dict[str, Any]:
    start = record.scheduled_start if record is not None else _start(game)
    base: dict[str, Any] = {
        "game_id": None if record is None else record.key.game_id,
        "start": start.isoformat(),
        "opponent": None if game is None else game.opponent,
        "opponent_image_url": None if game is None else team_image_url(game.opponent),
        "home": None if game is None else game.home,
        "score": None,
        "deadline": None,
        "overridable": False,
        "version": None,
    }
    if record is None:
        # A started game with no stored row was not on this roster when planning last ran.
        return {**base, "state": "upcoming" if start > now else "not_eligible"}
    state = game_state(record, now, records=records, sleeper_lock_date=sleeper_lock_date)
    score = record.stable_score
    if score is None and state.state in {"live", "locked"}:
        score = record.current_observed_score
    return {
        **base,
        "state": state.state,
        "score": score,
        "deadline": None if state.deadline is None else state.deadline.isoformat(),
        "overridable": state.overridable,
        "version": record.row_version if state.overridable else None,
    }


def _start(game: GameRow | None) -> datetime:
    assert game is not None
    return game.start


def _decisions(
    plan_trace: Mapping[str, Any] | None,
    names: Mapping[str, str | None],
    current: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    """One decision per player: a score action outranks a plain lineup move."""
    if plan_trace is None:
        return {}
    try:
        desired = {
            row["player_id"]: row["slot_position"]
            for row in plan_trace.get("desired_assignments", [])
            if row.get("player_id")
        }
        moves = list(plan_trace.get("moves", []))
        decisions: dict[str, dict[str, Any]] = {}
        for action in plan_trace.get("score_actions", []):
            player_id = action["player_id"]
            decisions.setdefault(player_id, _score_decision(action, desired, moves, names, current))
        for move in moves:
            player_id = move["player_id"]
            if player_id not in decisions:
                decisions[player_id] = _move_decision(move, desired)
        return decisions
    except (AttributeError, KeyError, TypeError):
        return {}


def _score_decision(
    action: Mapping[str, Any],
    desired: Mapping[str, str],
    moves: Sequence[Mapping[str, Any]],
    names: Mapping[str, str | None],
    current: Mapping[str, str],
) -> dict[str, Any]:
    player_id = action["player_id"]
    score = float(action["score"])
    decision: dict[str, Any] = {
        "game_id": action["game_id"],
        "score": score,
        "alternative_expected": _number(action.get("alternative_expected_score")),
        "confidence": _number(action.get("confidence")),
        "deadline": _text(action.get("deadline")),
    }
    if action["kind"] == "lock":
        return {**decision, "kind": "lock", "text": f"Lock {score:.1f}"}
    target = desired.get(player_id)
    if target is None:
        substitute = next(
            (
                names.get(move["player_id"]) or move["player_id"]
                for move in moves
                if move.get("swap_player_id") == player_id
            ),
            None,
        )
        bench = f"Bench for {substitute}" if substitute else "Bench"
        return {**decision, "kind": "sacrifice", "text": f"{bench} — gives up {score:.1f}"}
    text = (
        f"Keep starting — move to {target} to stay lockable"
        if current.get(player_id) != target
        else "Keep starting to stay lockable"
    )
    return {**decision, "kind": "preserve", "text": text}


def _move_decision(move: Mapping[str, Any], desired: Mapping[str, str]) -> dict[str, Any]:
    target = desired.get(move["player_id"])
    return {
        "kind": "move",
        "text": f"Start at {target}" if target else "Bench",
        "game_id": None,
        "score": None,
        "alternative_expected": None,
        "confidence": None,
        "deadline": _text(move.get("deadline")),
    }


def _with_opponent(decision: dict[str, Any], games: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    game = next((item for item in games if item["game_id"] == decision["game_id"]), None)
    if game is not None and game["opponent"] and decision["kind"] in {"lock", "sacrifice"}:
        decision["text"] = f"{decision['text']} vs {game['opponent']}"
    return decision


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None
