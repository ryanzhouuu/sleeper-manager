"""Join raw history with independently reviewed opportunities and export local evidence.

Reviewed absence from both game rosters is negative membership evidence, not a DNP.
Played/DNP outcomes must be explicit; supplemental records can only confirm DNPs.
"""

from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from sleeper_manager.backtesting.artifacts import atomic_write_bytes, canonical_json_bytes
from sleeper_manager.backtesting.replay.inputs.models import GameRosterCensus, SourceFingerprint
from sleeper_manager.domain.nba import GameStatus, ScheduledGame
from sleeper_manager.projections.hybrid_config import HybridProjectionError
from sleeper_manager.projections.hybrid_history_rows import (
    HistoryFinalization,
    HistoryOutcome,
    HybridHistoryGap,
    ParsedHybridHistory,
    history_evidence_version,
    validate_history_games,
)
from sleeper_manager.projections.hybrid_types import HybridHistory, ParticipationOpportunity


@dataclass(frozen=True, slots=True)
class HybridHistoryBuild:
    history: HybridHistory
    gaps: tuple[HybridHistoryGap, ...]
    source_versions: tuple[str, ...]
    census_sources: tuple[SourceFingerprint, ...]
    finalization_versions: tuple[str, ...]
    census_games: int
    reviewed_player_games: int
    expected_opportunities: int
    uncensused_outcomes: int

    @property
    def coverage(self) -> dict[str, object]:
        """Keep declared scope and unresolved evidence beside the consumable history."""
        return {
            "schema_version": "hybrid-history-coverage-v1",
            "dataset_version": self.history.dataset_version,
            "source_versions": self.source_versions,
            "census_sources": self.census_sources,
            "finalization_versions": self.finalization_versions,
            "counts": {
                "played_observations": len(self.history.observations),
                "census_games": self.census_games,
                "reviewed_player_games": self.reviewed_player_games,
                "expected_opportunities": self.expected_opportunities,
                "resolved_opportunities": len(self.history.opportunities),
                "uncensused_outcomes": self.uncensused_outcomes,
            },
            "gap_counts": dict(sorted(Counter(gap.reason for gap in self.gaps).items())),
            "gaps": self.gaps,
        }


def build_hybrid_history(
    sources: Sequence[ParsedHybridHistory],
    *,
    games: Sequence[ScheduledGame],
    resolved_player_ids: Collection[str],
    finalization: Mapping[str, HistoryFinalization],
    censuses: Sequence[GameRosterCensus],
    census_sources: Sequence[SourceFingerprint],
    supplemental_dnp: Sequence[HistoryOutcome] = (),
) -> HybridHistoryBuild:
    """Resolve only census-listed opportunities; reject duplicate or conflicting evidence.

    Use validated final-roster ledgers and their fingerprints. Uncensused outcomes may
    support stat history but never expand the participation denominator.
    """
    by_game = validate_history_games(games, finalization)
    resolved = set(resolved_player_ids)
    if any(not player.strip() for player in resolved):
        raise HybridProjectionError("missing_history_identity")
    if censuses and not census_sources:
        raise HybridProjectionError("missing_census_sources")
    source_versions = [source.source_version for source in sources]
    if any(not version.strip() for version in source_versions):
        raise HybridProjectionError("missing_history_source")
    if len(set(source_versions)) != len(source_versions):
        raise HybridProjectionError("duplicate_history_source")
    observations = tuple(
        sorted(
            (row for source in sources for row in source.observations),
            key=lambda row: (row.game_start, row.game_id, row.player_id),
        )
    )
    gaps = {gap for source in sources for gap in source.gaps}
    outcomes: dict[tuple[str, str], HistoryOutcome] = {}
    for source in sources:
        for outcome in source.outcomes:
            key = outcome.game_id, outcome.player_id
            if key in outcomes:
                raise HybridProjectionError("duplicate_history_outcome")
            outcomes[key] = outcome
    supplements: set[tuple[str, str]] = set()
    for outcome in sorted(supplemental_dnp, key=lambda row: (row.game_id, row.player_id)):
        key = outcome.game_id, outcome.player_id
        if outcome.did_play:
            raise HybridProjectionError("supplement_requires_explicit_dnp")
        if key in supplements:
            raise HybridProjectionError("duplicate_supplemental_outcome")
        supplements.add(key)
        prior = outcomes.get(key)
        if prior is not None and (prior.did_play or prior.team_id != outcome.team_id):
            raise HybridProjectionError("conflicting_history_outcome")
        outcomes[key] = (
            replace(
                outcome,
                source_version=history_evidence_version(
                    prior.source_version,
                    outcome.source_version,
                ),
            )
            if prior is not None
            else outcome
        )
    for outcome in outcomes.values():
        game = by_game.get(outcome.game_id)
        if game is None or game.status is not GameStatus.FINAL:
            raise HybridProjectionError("outcome_without_final_game")
        if outcome.player_id not in resolved:
            raise HybridProjectionError("unresolved_outcome_identity")
        if outcome.team_id not in (game.home_team_id, game.away_team_id):
            raise HybridProjectionError("outcome_team_mismatch")
    for row in observations:
        observed_outcome = outcomes.get((row.game_id, row.player_id))
        timing = finalization.get(row.game_id)
        if (
            observed_outcome is None
            or not observed_outcome.did_play
            or not row.did_play
            or timing is None
            or row.game_start != by_game[row.game_id].start_time
            or row.finalized_at != timing.finalized_at
        ):
            raise HybridProjectionError("history_observation_mismatch")
    ordered_censuses = tuple(
        sorted(
            (
                replace(
                    census,
                    reviewed_player_ids=tuple(sorted(census.reviewed_player_ids)),
                    player_teams=tuple(sorted(census.player_teams)),
                )
                for census in censuses
            ),
            key=lambda census: census.game_id,
        )
    )
    if len({census.game_id for census in censuses}) != len(censuses):
        raise HybridProjectionError("duplicate_history_census")
    opportunities: list[ParticipationOpportunity] = []
    expected_keys: set[tuple[str, str]] = set()
    for census in ordered_censuses:
        game = by_game.get(census.game_id)
        if (
            game is None
            or game.status is not GameStatus.FINAL
            or (census.home_team_id, census.away_team_id) != (game.home_team_id, game.away_team_id)
        ):
            raise HybridProjectionError("census_game_mismatch")
        listed = dict(census.player_teams)
        for player in census.reviewed_player_ids:
            census_outcome = outcomes.get((census.game_id, player))
            if census_outcome is not None and listed.get(player) != census_outcome.team_id:
                raise HybridProjectionError("census_outcome_mismatch")
            if player not in resolved:
                gaps.add(HybridHistoryGap(census.game_id, player, "unresolved_player"))
        for player, _ in census.player_teams:
            key = census.game_id, player
            expected_keys.add(key)
            if player not in resolved:
                continue
            census_outcome = outcomes.get(key)
            timing = finalization.get(census.game_id)
            if census_outcome is None:
                gaps.add(HybridHistoryGap(*key, "missing_outcome"))
            if timing is None:
                gaps.add(HybridHistoryGap(*key, "missing_finalization"))
            if census_outcome is not None and timing is not None:
                opportunities.append(
                    ParticipationOpportunity(
                        player,
                        census.game_id,
                        game.start_time,
                        timing.finalized_at,
                        census_outcome.did_play,
                        history_evidence_version(
                            census_outcome.source_version, census, timing.source_version
                        ),
                    )
                )
    ordered_gaps = tuple(sorted(gaps))
    ordered_sources = tuple(
        sorted(set(source_versions + [row.source_version for row in supplemental_dnp]))
    )
    ordered_census_sources = tuple(
        sorted(
            set(census_sources),
            key=lambda source: (
                source.name,
                source.content_hash,
                source.version,
            ),
        )
    )
    ordered_opportunities = tuple(
        sorted(
            opportunities,
            key=lambda row: (
                row.game_start,
                row.game_id,
                row.player_id,
            ),
        )
    )
    schedule = tuple(
        sorted(
            (game.provider_id, game.start_time, game.status, game.home_team_id, game.away_team_id)
            for game in games
        )
    )
    dataset_version = history_evidence_version(
        "hybrid-history-adapter-v1",
        observations,
        ordered_opportunities,
        ordered_gaps,
        ordered_sources,
        ordered_census_sources,
        ordered_censuses,
        tuple(sorted(outcomes.values(), key=lambda row: (row.game_id, row.player_id))),
        schedule,
        tuple(sorted(finalization.items())),
        tuple(sorted(resolved)),
    )
    return HybridHistoryBuild(
        HybridHistory(dataset_version, observations, ordered_opportunities),
        ordered_gaps,
        ordered_sources,
        ordered_census_sources,
        tuple(sorted({evidence.source_version for evidence in finalization.values()})),
        len(censuses),
        sum(len(census.reviewed_player_ids) for census in censuses),
        len(expected_keys),
        len(outcomes.keys() - expected_keys),
    )


def write_hybrid_history_build(output_dir: Path, build: HybridHistoryBuild) -> None:
    """Write immutable history/coverage files; identical reruns preserve existing bytes."""
    outputs = {
        output_dir / "history.json": canonical_json_bytes(build.history),
        output_dir / "coverage.json": canonical_json_bytes(build.coverage),
    }
    for path, payload in outputs.items():
        if path.exists() and path.read_bytes() != payload:
            raise HybridProjectionError("history_output_conflict")
    for path, payload in outputs.items():
        if not path.exists():
            atomic_write_bytes(path, payload)
