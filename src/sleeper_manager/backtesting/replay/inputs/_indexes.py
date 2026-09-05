"""Deduplicate replay join keys and retain conflicting source rows as exclusions."""

from collections import defaultdict
from collections.abc import Sequence

from sleeper_manager.backtesting.replay.inputs.models import ReplayInputExclusion
from sleeper_manager.domain.nba import PlayerBoxScore, ScheduledGame
from sleeper_manager.domain.planning import PlanningReasonCode
from sleeper_manager.integrations.nba.identity import PlayerMapping


def _index_schedules(
    games: Sequence[ScheduledGame],
) -> tuple[dict[str, ScheduledGame], tuple[ReplayInputExclusion, ...]]:
    indexed: dict[str, ScheduledGame] = {}
    issues: list[ReplayInputExclusion] = []
    for game in games:
        previous = indexed.get(game.provider_id)
        if previous is None:
            indexed[game.provider_id] = game
            continue
        if previous != game:
            issues.append(
                ReplayInputExclusion(
                    PlanningReasonCode.AMBIGUOUS_EVENT_ORDER,
                    f"game={game.provider_id}",
                    "Historical schedule contains conflicting rows for one game ID.",
                )
            )
    return indexed, tuple(issues)


def _index_box_scores(
    box_scores: Sequence[PlayerBoxScore],
) -> tuple[tuple[PlayerBoxScore, ...], tuple[ReplayInputExclusion, ...]]:
    indexed: dict[tuple[str, str], PlayerBoxScore] = {}
    issues: list[ReplayInputExclusion] = []
    for box_score in box_scores:
        key = box_score.game_id, box_score.player_id
        previous = indexed.get(key)
        if previous is None:
            indexed[key] = box_score
            continue
        if previous != box_score:
            issues.append(
                ReplayInputExclusion(
                    PlanningReasonCode.AMBIGUOUS_EVENT_ORDER,
                    f"game={box_score.game_id}:provider-player={box_score.player_id}",
                    "Historical box scores contain conflicting rows for one player-game.",
                )
            )
    return tuple(indexed.values()), tuple(issues)


def _index_mappings(
    mappings: Sequence[PlayerMapping],
) -> tuple[dict[str, PlayerMapping], tuple[ReplayInputExclusion, ...]]:
    by_provider: dict[str, list[PlayerMapping]] = defaultdict(list)
    issues: list[ReplayInputExclusion] = []
    for mapping in mappings:
        if mapping.espn_id is None:
            issues.append(
                ReplayInputExclusion(
                    PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY,
                    f"sleeper-player={mapping.sleeper_id}",
                    mapping.reason,
                )
            )
            continue
        by_provider[mapping.espn_id].append(mapping)

    resolved: dict[str, PlayerMapping] = {}
    for provider_id, candidates in by_provider.items():
        unique = tuple(dict.fromkeys(candidates))
        if len({candidate.sleeper_id for candidate in unique}) != 1:
            issues.append(
                ReplayInputExclusion(
                    PlanningReasonCode.UNRESOLVED_PLAYER_IDENTITY,
                    f"provider-player={provider_id}",
                    "Provider player ID maps to multiple Sleeper players.",
                )
            )
            continue
        resolved[provider_id] = unique[0]
    return resolved, tuple(issues)
