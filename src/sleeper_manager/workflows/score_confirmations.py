"""Keep per-player confirmation identities inside a coordinated team recommendation."""

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any

from sleeper_manager.domain.lock_in import LockInDecisionKind, LockInOpportunityStatus
from sleeper_manager.domain.planning import WeeklyPlan
from sleeper_manager.persistence.base import (
    AsyncStateRepository,
    RecommendationRecord,
    RecommendationStatus,
)
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityKey
from sleeper_manager.workflows.notification_loop import recommendation_id_for


async def prepare_score_confirmations(
    repository: AsyncStateRepository,
    plan: WeeklyPlan,
    *,
    message: str,
    trace_json: str,
    now: datetime,
    player_names: dict[str, str] | None = None,
) -> tuple[RecommendationRecord, ...]:
    """Create auditable child actions; delivery and confirmation remain separate operations."""
    pending = await repository.list_pending_recommendations(
        plan.league_id, plan.week, decision_type="live_lock_in"
    )
    results = []
    parent_trace = json.loads(trace_json)
    material = parent_trace.get("availability_material_hash", plan.material_hash)
    names = player_names or {}
    for target in _confirmation_targets(plan):
        key = (
            f"{plan.league_id}:{plan.week}:{plan.roster_id}:team-score:"
            f"{material}:{target.player_id}:{target.game_id}"
        )
        base = key
        revision = 0
        while True:
            existing = await repository.get_recommendation(recommendation_id_for(key))
            if existing is None or existing.status is RecommendationStatus.PENDING:
                break
            revision += 1
            key = f"{base}:r{revision}"
        player_name = names.get(target.player_id, target.player_id)
        trace = {
            "kind": target.kind,
            "player_name": player_name,
            "observed_score": target.score,
            "expected_terminal_score": plan.expected_terminal_score,
            "acknowledgement": target.acknowledgement,
            "team_plan": parent_trace,
            "availability_notes": parent_trace.get("availability_notes", []),
            **target.extra,
        }
        child = RecommendationRecord(
            recommendation_id=recommendation_id_for(key),
            idempotency_key=key,
            league_id=plan.league_id,
            fantasy_week=plan.week,
            player_id=target.player_id,
            game_id=target.game_id,
            decision_type="live_lock_in",
            title=target.title(player_name),
            message=message,
            deadline=target.deadline,
            policy_version=plan.manager_policy_version,
            created_at=now,
            trace_json=json.dumps(trace, sort_keys=True, separators=(",", ":")),
        )
        for old in pending:
            if (
                old.player_id == target.player_id
                and old.game_id == target.game_id
                and old.recommendation_id != child.recommendation_id
            ):
                await repository.supersede_recommendation(old.recommendation_id, now)
        await repository.create_recommendation(child)
        child = await repository.get_recommendation(child.recommendation_id) or child
        results.append(child)
        await _mark_actionable(
            repository, child, plan.roster_id, material_hash=plan.material_hash, now=now
        )
    current_ids = {row.recommendation_id for row in results}
    for old in pending:
        if old.recommendation_id not in current_ids:
            await repository.supersede_recommendation(old.recommendation_id, now)
    return tuple(results)


UP_TO_YOU_KIND = "up_to_you"


@dataclass(frozen=True, slots=True)
class _ConfirmationTarget:
    player_id: str
    game_id: str
    kind: str
    score: float | None
    deadline: datetime | None
    acknowledgement: dict[str, object]
    extra: dict[str, object]

    def title(self, player_name: str) -> str:
        if self.kind == UP_TO_YOU_KIND:
            return f"Lock or pass {player_name}?"
        return f"{self.kind.title()} {player_name}?"


def _confirmation_targets(plan: WeeklyPlan) -> tuple[_ConfirmationTarget, ...]:
    """Advised Lock/Pass actions, then close calls left to the manager."""
    targets = []
    for action in plan.score_actions:
        fragment: dict[str, object] = {"schema_version": 1}
        if action.kind is LockInDecisionKind.LOCK:
            fragment.update(
                slot_index=action.slot_index,
                slot_position=action.slot_position,
                accepted_fantasy_score=action.score,
            )
        targets.append(
            _ConfirmationTarget(
                action.player_id,
                action.game_id,
                action.kind.value,
                action.score,
                action.deadline,
                fragment,
                {},
            )
        )
    for row in plan.undecided_scores:
        choice = {
            name: value.isoformat() if isinstance(value, datetime) else value
            for name, value in asdict(row).items()
        }
        targets.append(
            _ConfirmationTarget(
                row.player_id,
                row.game_id,
                UP_TO_YOU_KIND,
                row.score,
                row.deadline,
                {"schema_version": 1},
                {"choice": choice},
            )
        )
    return tuple(targets)


async def restore_score_confirmations(
    repository: AsyncStateRepository, trace: Mapping[str, Any], *, now: datetime
) -> tuple[RecommendationRecord, ...]:
    """Each poll resets its finals to Finalizing, so advice kept in place must re-mark them."""
    children = []
    for identity in trace.get("confirmation_targets", ()):
        child = await repository.get_recommendation(identity)
        if child is None or child.status is not RecommendationStatus.PENDING:
            continue
        await _mark_actionable(
            repository, child, trace["roster_id"], material_hash=trace["material_hash"], now=now
        )
        children.append(child)
    return tuple(children)


async def _mark_actionable(
    repository: AsyncStateRepository,
    child: RecommendationRecord,
    roster_id: int,
    *,
    material_hash: str,
    now: datetime,
) -> None:
    getter = getattr(repository, "get_lock_in_opportunity", None)
    updater = getattr(repository, "update_lock_in_opportunity", None)
    if getter is None or updater is None or child.game_id is None:
        return
    row = await getter(
        LockInOpportunityKey(
            child.league_id, child.fantasy_week, roster_id, child.player_id, child.game_id
        )
    )
    if row is not None and (
        row.status is LockInOpportunityStatus.AUTOMATIC_FINAL
        or row.status
        not in {
            LockInOpportunityStatus.ACKNOWLEDGED_LOCKED,
            LockInOpportunityStatus.ACKNOWLEDGED_PASSED,
            LockInOpportunityStatus.AUTOMATIC_FINAL,
            LockInOpportunityStatus.INELIGIBLE,
        }
    ):
        await updater(
            replace(
                row,
                status=LockInOpportunityStatus.ACTIONABLE,
                current_recommendation_id=child.recommendation_id,
                current_recommendation_kind=child.decision_type,
                trace_json=child.trace_json,
                latest_evaluation_hash=material_hash,
                updated_at=now,
            ),
            expected_version=row.row_version,
        )
