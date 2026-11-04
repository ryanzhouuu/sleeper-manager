"""Identify everything a lineup decision reads, so unchanged inputs need no new search."""

from dataclasses import fields, is_dataclass, replace
from hashlib import sha256

from sleeper_manager.decisions._weekly_plan_inputs import future_opportunities
from sleeper_manager.decisions._weekly_plan_models import WEEKLY_PLANNER_VERSION
from sleeper_manager.decisions.completed_scores import usable_completed_games
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.domain.planning import FreshnessSummary, TeamWeekState

_RECENCY_FIELDS = frozenset(
    {"availability_evidence_at", "available_as_of", "decision_time", "retrieved_at"}
)


def decision_input_fingerprint(state: TeamWeekState, policy: WeeklyPlanPolicyConfig) -> str:
    """Stale sources surface as blocking reasons, so their retrieval times can be ignored."""
    usable = tuple((row.sleeper_player_id, row.game_id) for row in usable_completed_games(state))
    upcoming = tuple(
        (row.sleeper_player_id, row.game_id)
        for row in future_opportunities(state, state.decision_time)
    )
    payload = (
        WEEKLY_PLANNER_VERSION,
        repr(policy),
        usable,
        upcoming,
        _canonical(replace(state, freshness=FreshnessSummary())),
    )
    return sha256(repr(payload).encode()).hexdigest()


def _canonical(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return tuple(
            (field.name, _canonical(getattr(value, field.name)))
            for field in fields(value)
            if field.name not in _RECENCY_FIELDS
        )
    if isinstance(value, tuple):
        return tuple(_canonical(item) for item in value)
    return value
