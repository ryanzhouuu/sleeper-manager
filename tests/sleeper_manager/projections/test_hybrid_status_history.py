"""Retrospective status buckets must preserve census outcomes and temporal provenance."""

from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.backtesting.replay.inputs import SourceFingerprint
from sleeper_manager.backtesting.replay.inputs.models import GameRosterCensus
from sleeper_manager.domain.nba import AvailabilityStatus, Team
from sleeper_manager.projections.hybrid_config import HybridProjectionConfig
from sleeper_manager.projections.hybrid_participation import HybridParticipation
from sleeper_manager.projections.hybrid_status_history import attach_hybrid_status_history
from sleeper_manager.projections.hybrid_types import (
    GameAvailability,
    HybridHistory,
    ParticipationOpportunity,
)
from tests.sleeper_manager.workflows.test_game_availability import evidence


def inputs():  # type: ignore[no-untyped-def]
    game, player, report = evidence(status=AvailabilityStatus.QUESTIONABLE)
    row = ParticipationOpportunity(
        player.provider_id,
        game.provider_id,
        game.start_time,
        game.start_time + timedelta(hours=3),
        False,
        "resolved-census",
    )
    census = GameRosterCensus(
        game.provider_id,
        game.home_team_id,
        game.away_team_id,
        (player.provider_id,),
        ((player.provider_id, game.home_team_id),),
        "verified-final",
    )
    report = replace(
        report, source=replace(report.source, retrieved_at=game.start_time + timedelta(days=100))
    )
    kwargs = dict(
        games=(game,),
        players=(replace(player, team_id="traded", team_abbreviation="LAL"),),
        teams=(Team(game.home_team_id, "CHI", "Chicago Bulls", None, player.source),),
        censuses=(census,),
        reports={game.provider_id: report},
        sources=(SourceFingerprint("report", "abc", "pdf-v1"),),
    )
    return HybridHistory("original", (), (row,)), kwargs


def test_tags_use_historical_census_team_and_preserve_retrieval_provenance() -> None:
    history, kwargs = inputs()
    result = attach_hybrid_status_history(history, **kwargs)
    assert result.added_count == 1
    assert result.history.observations is history.observations
    original, tagged = history.opportunities[0], result.history.opportunities[0]
    assert replace(tagged, availability=None) == original
    assert tagged.availability is not None
    assert tagged.availability.bucket == "reported:questionable"
    assert tagged.availability.observed_at == kwargs["reports"][tagged.game_id].published_at
    assert not tagged.availability.confirmed_absence
    assert "reconstructed-official:archive-retrieved:" in tagged.availability.source_version
    assert result.history.dataset_version != history.dataset_version
    assert (
        result.coverage["evidence_semantics"]
        == "reconstructed_official_publication_not_prospective_capture"
    )
    assert result == attach_hybrid_status_history(history, **kwargs)


@pytest.mark.parametrize(
    "gap",
    [
        "missing_report",
        "missing_census",
        "missing_player_identity",
        "stale",
        "future",
        "wrong_game",
    ],
)
def test_missing_or_inapplicable_sources_preserve_untagged_denominator(gap: str) -> None:
    history, kwargs = inputs()
    if gap.startswith("missing_"):
        key = {
            "missing_report": "reports",
            "missing_census": "censuses",
            "missing_player_identity": "players",
        }[gap]
        kwargs[key] = {} if key == "reports" else ()
    else:
        report = kwargs["reports"][history.opportunities[0].game_id]
        report = (
            replace(
                report,
                published_at=history.opportunities[0].game_start
                + timedelta(days=-2 if gap == "stale" else 1),
            )
            if gap != "wrong_game"
            else replace(report, team_statuses=())
        )
        kwargs["reports"] = {history.opportunities[0].game_id: report}
    result = attach_hybrid_status_history(history, **kwargs)
    assert result.history == history
    assert result.added_count == 0
    assert len(result.gaps) == 1


def test_tagged_outcomes_supply_status_bucket_instead_of_history_fallback() -> None:
    history, kwargs = inputs()
    result = attach_hybrid_status_history(history, **kwargs)
    row = result.history.opportunities[0]
    assert row.availability is not None
    cutoff = row.game_start + timedelta(days=1)
    untagged_played = replace(row, player_id="other", did_play=True, availability=None)
    model = HybridParticipation(
        replace(result.history, opportunities=(row, untagged_played)),
        cutoff=cutoff,
        game_start=cutoff + timedelta(hours=1),
        config=HybridProjectionConfig(),
    )
    current = GameAvailability(
        "next", cutoff, AvailabilityStatus.QUESTIONABLE, row.availability.observation, "live"
    )
    estimate = model.estimate("new", game_id="next", availability=current)
    assert estimate.pooled_bucket_count == 1
    assert estimate.probability < estimate.history_probability
    assert estimate.fallback is None
