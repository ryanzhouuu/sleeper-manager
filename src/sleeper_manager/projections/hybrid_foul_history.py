"""Reconcile official summaries with attributed source events before adding coverage.

Callers verify retained report hashes and supply explicit complete-summary review.
Unresolved or contradictory games fail without changing the original history.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from sleeper_manager.backtesting.replay.final_foul_text import FinalFoulText, FoulKind
from sleeper_manager.integrations.nba.mapping import normalize_player_name
from sleeper_manager.projections.hybrid_config import HybridProjectionError
from sleeper_manager.projections.hybrid_history_rows import history_evidence_version
from sleeper_manager.projections.hybrid_types import HybridHistory, require_aware

FOUL_FIELDS = ("technical_fouls", "flagrant_fouls")


@dataclass(frozen=True, slots=True)
class FoulPlayerIdentity:
    """Bind a provider identity to a named box row on one historical game side."""

    player_id: str
    full_name: str
    side: int


@dataclass(frozen=True, slots=True)
class FoulActorEvidence:
    """One independently attributed assessment; None identifies a verified nonplayer."""

    full_name: str
    player_id: str | None
    side: int
    kind: FoulKind
    clock: str
    period: int
    source_version: str


@dataclass(frozen=True, slots=True)
class QualifiedGameFouls:
    """Complete per-played-player counts and explicit source/timing provenance."""

    game_id: str
    game_start: datetime
    finalized_at: datetime
    player_counts: tuple[tuple[str, int, int], ...]
    source_version: str


def qualify_final_game_fouls(
    report: FinalFoulText,
    *,
    game_id: str,
    game_start: datetime,
    finalized_at: datetime,
    players: Sequence[FoulPlayerIdentity],
    actors: Sequence[FoulActorEvidence],
    report_source_version: str,
    complete_summary_review: str,
) -> QualifiedGameFouls:
    """Require complete review and one-to-one name, side, clock, and subtype reconciliation.

    The review explicitly covers omitted sections. A coach or team assessment never
    enters a player count. Ambiguous roster names and unmatched events reject the game.
    """
    require_aware(game_start)
    require_aware(finalized_at)
    if not all((game_id.strip(), report_source_version.strip(), complete_summary_review.strip())):
        raise HybridProjectionError("missing_foul_coverage_review")
    if finalized_at < game_start:
        raise HybridProjectionError("invalid_foul_finalization")
    by_player = {row.player_id: row for row in players}
    if len(by_player) != len(players) or any(
        not row.player_id.strip() or not row.full_name.strip() or row.side not in (0, 1)
        for row in players
    ):
        raise HybridProjectionError("invalid_foul_player_identity")
    by_name: dict[tuple[int, str], list[str]] = {}
    for row in players:
        by_name.setdefault((row.side, normalize_player_name(row.full_name)), []).append(
            row.player_id
        )
    played: set[str] = set()
    for box_row in report.roster.rows:
        if not box_row.did_play:
            continue
        candidates = by_name.get((box_row.side, normalize_player_name(box_row.name)), [])
        if len(candidates) != 1 or candidates[0] in played:
            raise HybridProjectionError("unresolved_foul_box_identity")
        played.add(candidates[0])
    remaining = list(actors)
    counts: Counter[tuple[str, str]] = Counter()
    for event in report.events:
        matches = [
            index
            for index, actor in enumerate(remaining)
            if (actor.side, actor.kind, actor.clock, actor.period)
            == (event.side, event.kind, event.clock, event.period)
            and _matches_name(event.name, actor.full_name)
        ]
        identities = {(remaining[index].player_id, remaining[index].full_name) for index in matches}
        if not matches or len(identities) != 1:
            raise HybridProjectionError("unresolved_foul_actor")
        actor = remaining.pop(matches[0])
        if not actor.source_version.strip():
            raise HybridProjectionError("missing_foul_actor_source")
        if actor.player_id is None:
            if by_name.get((actor.side, normalize_player_name(actor.full_name))):
                raise HybridProjectionError("foul_nonplayer_conflicts_with_roster")
            continue
        identity = by_player.get(actor.player_id)
        if identity is None or (identity.side, normalize_player_name(identity.full_name)) != (
            actor.side,
            normalize_player_name(actor.full_name),
        ):
            raise HybridProjectionError("foul_actor_identity_mismatch")
        if actor.player_id in played:
            counts[actor.player_id, event.field] += 1
    if remaining:
        raise HybridProjectionError("foul_source_count_mismatch")
    values = tuple(
        (player, counts[player, "technical_fouls"], counts[player, "flagrant_fouls"])
        for player in sorted(played)
    )
    return QualifiedGameFouls(
        game_id,
        game_start,
        finalized_at,
        values,
        history_evidence_version(
            "official-final-fouls-v1",
            report_source_version,
            complete_summary_review,
            tuple(sorted(actors, key=lambda row: repr(row))),
            values,
        ),
    )


def attach_hybrid_foul_history(
    history: HybridHistory, evidence: Sequence[QualifiedGameFouls]
) -> HybridHistory:
    """Add only qualified fields; preserve census and reject conflicting verified values."""
    games = {game.game_id: game for game in evidence}
    if len(games) != len(evidence):
        raise HybridProjectionError("duplicate_foul_game_evidence")
    counts = {}
    for game in evidence:
        require_aware(game.game_start)
        require_aware(game.finalized_at)
        if not game.game_id.strip() or not game.source_version.strip():
            raise HybridProjectionError("invalid_qualified_foul_evidence")
        for player, technical, flagrant in game.player_counts:
            key = game.game_id, player
            if (
                key in counts
                or not player.strip()
                or any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in (technical, flagrant)
                )
            ):
                raise HybridProjectionError("invalid_qualified_foul_counts")
            counts[key] = (technical, flagrant)
    rows = []
    for row in history.observations:
        values = counts.get((row.game_id, row.player_id))
        if values is None:
            rows.append(row)
            continue
        game = games[row.game_id]
        if row.game_start != game.game_start or game.finalized_at < row.game_start:
            raise HybridProjectionError("foul_history_game_mismatch")
        changed = dict(zip(FOUL_FIELDS, values, strict=True))
        if any(
            field in row.verified_fields and getattr(row.line, field) != value
            for field, value in changed.items()
        ):
            raise HybridProjectionError("conflicting_verified_foul_count")
        rows.append(
            replace(
                row,
                line=replace(row.line, **changed),
                verified_fields=tuple(set(row.verified_fields) | set(FOUL_FIELDS)),
                finalized_at=max(row.finalized_at, game.finalized_at),
                source_version=history_evidence_version(row.source_version, game.source_version),
            )
        )
    return HybridHistory(
        history_evidence_version(
            history.dataset_version, tuple(sorted(evidence, key=lambda row: row.game_id))
        ),
        tuple(rows),
        history.opportunities,
    )


def _matches_name(short: str, full: str) -> bool:
    """Permit whole-token suffix matches; qualification rejects ambiguous witnesses."""
    short_name = normalize_player_name(short)
    full_name = normalize_player_name(full)
    return full_name == short_name or full_name.endswith(" " + short_name)
