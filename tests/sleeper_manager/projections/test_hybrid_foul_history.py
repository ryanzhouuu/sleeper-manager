"""Qualify complete player-level foul evidence without inventing zero coverage."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sleeper_manager.backtesting.replay.final_foul_text import parse_final_foul_text
from sleeper_manager.domain.scoring import BoxScoreLine
from sleeper_manager.projections.hybrid_config import HybridProjectionError
from sleeper_manager.projections.hybrid_foul_history import (
    FoulActorEvidence,
    FoulPlayerIdentity,
    attach_hybrid_foul_history,
    qualify_final_game_fouls,
)
from sleeper_manager.projections.hybrid_types import (
    HybridHistory,
    JointGameObservation,
    ParticipationOpportunity,
)

START = datetime(2025, 1, 1, 23, tzinfo=UTC)
FINAL = START + timedelta(hours=4)
PLAYERS = (FoulPlayerIdentity("1", "Alex Example", 0), FoulPlayerIdentity("2", "Bailey Sample", 1))
TEXT = """OFFICIAL SCORER'S REPORT FINAL BOX
VISITOR: Example Visitors (1-0)
1 Alex Example    G  30:00  10
HOME: Sample Hosts (0-1)
2 Bailey Sample   G  30:00  10
SCORE BY PERIOD
{summary}
Copyright (c) 2024-2025 NBA Properties, INC. All Rights Reserved
"""


def qualify(summary: str = "", actors: tuple[FoulActorEvidence, ...] = (), **changes):  # type: ignore[no-untyped-def]
    """Make the explicit omission review visible at every qualification boundary."""
    values = dict(
        game_id="game",
        game_start=START,
        finalized_at=FINAL,
        players=PLAYERS,
        actors=actors,
        report_source_version="report-sha256",
        complete_summary_review="hash-verified-complete-summary-and-pbp-reconciliation-v1",
    )
    values.update(changes)
    return qualify_final_game_fouls(parse_final_foul_text(TEXT.format(summary=summary)), **values)


def actor(name: str = "Alex Example", player: str | None = "1", **changes):  # type: ignore[no-untyped-def]
    """Preserve the independent actor witness rather than inferring from a surname."""
    values = dict(
        full_name=name,
        player_id=player,
        side=0,
        kind="individual",
        clock="1:00",
        period=1,
        source_version="raw-pbp-sha256",
    )
    values.update(changes)
    return FoulActorEvidence(**values)


def test_reviewed_omissions_supply_zero_counts_for_explicit_played_roster() -> None:
    """Missing source columns gain coverage only after complete report qualification."""
    evidence = qualify()
    assert evidence.player_counts == (("1", 0, 0), ("2", 0, 0))
    with pytest.raises(HybridProjectionError, match="missing_foul_coverage_review"):
        qualify(complete_summary_review="")


def test_reconcile_repeated_player_coach_team_and_flagrant_assessments() -> None:
    """Each assessment counts once; bench and team totals never penalize a player."""
    summary = """Technical fouls - Individual
Visitors (3): Example 1:00 1st , Example 1:00 1st , Coach 1:00 1st
Hosts (0): NONE
Technical fouls - Non-Unsportsmanlike
Visitors (1): Team 1:00 1st (Delay)
Hosts (0): NONE
Flagrant Fouls
Visitors (1): 1:00 1st Example-FLG2
Hosts (0): NONE"""
    actors = (
        actor(),
        actor(),
        actor("Casey Coach", None),
        actor("Team", None, kind="non_unsportsmanlike"),
        actor(kind="flagrant"),
    )
    evidence = qualify(summary, actors)
    assert evidence.player_counts == (("1", 2, 1), ("2", 0, 0))
    assert qualify(summary, tuple(reversed(actors))) == evidence


@pytest.mark.parametrize(
    "witness",
    [
        actor(clock="2:00"),
        actor(side=1),
        actor(period=2),
        actor(kind="flagrant"),
        actor("Wrong Person"),
        actor(player="wrong"),
        actor(source_version=""),
        actor(player=None),
    ],
)
def test_reject_actor_and_source_contradictions(witness: FoulActorEvidence) -> None:
    """A source event needs the same actor and exact game-relative timing."""
    with pytest.raises(HybridProjectionError):
        qualify(
            "Technical fouls - Individual\nVisitors (1): Example 1:00 1st\nHosts (0): NONE",
            (witness,),
        )


def test_unmatched_source_events_cannot_certify_omitted_summary_zeros() -> None:
    """Contradictory zero evidence blocks qualification of the entire game."""
    with pytest.raises(HybridProjectionError, match="foul_source_count_mismatch"):
        qualify(actors=(actor(),))


def test_ambiguous_short_names_require_independent_full_actor_identity() -> None:
    """A coach sharing a player's surname must not be assumed to be that player."""
    summary = "Technical fouls - Individual\nVisitors (1): Example 1:00 1st\nHosts (0): NONE"
    evidence = qualify(summary, (actor("Coach Example", None),))
    assert evidence.player_counts == (("1", 0, 0), ("2", 0, 0))
    with pytest.raises(HybridProjectionError, match="unresolved_foul_actor"):
        qualify(summary, (actor(), actor("Coach Example", None)))


@pytest.mark.parametrize(
    "players", [PLAYERS[:1], PLAYERS + PLAYERS[:1], (replace(PLAYERS[0], side=1), PLAYERS[1])]
)
def test_box_roster_requires_unique_historical_player_associations(players) -> None:  # type: ignore[no-untyped-def]
    """Current-team identity alone cannot resolve an official historical box row."""
    with pytest.raises(HybridProjectionError):
        qualify(players=players)


def history() -> HybridHistory:
    """Keep a preexisting stat gap and status-free census independent of foul coverage."""
    observation = JointGameObservation(
        "1",
        "game",
        START,
        FINAL,
        True,
        30,
        BoxScoreLine(points=20, technical_fouls=9),
        ("points",),
        "raw-box",
    )
    return HybridHistory(
        "original",
        (observation, replace(observation, game_id="unqualified")),
        (ParticipationOpportunity("1", "game", START, FINAL, True, "census"),),
    )


def test_attach_preserves_census_unqualified_rows_and_unrelated_gaps() -> None:
    """Qualified zero replaces an unverified default without altering other history."""
    original = history()
    evidence = qualify()
    augmented = attach_hybrid_foul_history(original, (evidence,))
    row = augmented.observations[0]
    assert row.line.technical_fouls == row.line.flagrant_fouls == 0
    assert set(row.verified_fields) == {"points", "technical_fouls", "flagrant_fouls"}
    assert augmented.opportunities is original.opportunities
    assert augmented.observations[1] == original.observations[1]
    assert row.line.points == 20 and row.minutes == 30
    assert augmented.dataset_version != original.dataset_version
    assert attach_hybrid_foul_history(original, (evidence,)) == augmented


def test_foul_evidence_cannot_be_visible_before_its_declared_finalization() -> None:
    """Later source finalization moves the augmented stat observation's visibility."""
    evidence = replace(qualify(), finalized_at=FINAL + timedelta(days=1))
    assert (
        attach_hybrid_foul_history(history(), (evidence,)).observations[0].finalized_at
        == evidence.finalized_at
    )


def test_reject_conflicting_verified_fouls_duplicate_evidence_and_wrong_games() -> None:
    """Do not overwrite verified facts or join a different game's report."""
    original = history()
    verified = replace(original.observations[0], verified_fields=("points", "technical_fouls"))
    with pytest.raises(HybridProjectionError, match="conflicting_verified_foul_count"):
        attach_hybrid_foul_history(replace(original, observations=(verified,)), (qualify(),))
    with pytest.raises(HybridProjectionError, match="duplicate_foul_game_evidence"):
        attach_hybrid_foul_history(original, (qualify(), qualify()))
    with pytest.raises(HybridProjectionError, match="foul_history_game_mismatch"):
        attach_hybrid_foul_history(
            original, (replace(qualify(), game_start=START + timedelta(minutes=1)),)
        )
