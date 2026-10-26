"""Grouped delivery retains game scope while traces preserve individual warnings."""

from dataclasses import replace
from datetime import timedelta
from zoneinfo import ZoneInfo

from sleeper_manager.domain.planning import GameOpportunity, PlanningReasonCode
from sleeper_manager.domain.projection import ProjectionReason
from sleeper_manager.workflows.availability_advice import (
    availability_material_hash,
    state_availability_notes,
)
from sleeper_manager.workflows.availability_rendering import state_availability_summary
from tests.sleeper_manager.decisions.weekly_plan_support import NOW, _opportunity, _state

START = NOW + timedelta(hours=14)
CHICAGO = ZoneInfo("America/Chicago")
NAMES = {"p1": "Ann", "p2": "Ben"}
MISSING_REPORT = "The team has not submitted a game report; participation remains provisional."


def _warning(
    player: str,
    game: str,
    code: str,
    message: str,
    *,
    hours_later: int = 0,
) -> GameOpportunity:
    """Attach visible and private-source reasons to an otherwise projected game."""
    opportunity = _opportunity(
        player, game, START + timedelta(hours=hours_later), ("G",), ((10, 1),)
    )
    assert opportunity.projection is not None
    return replace(
        opportunity,
        projection=replace(
            opportunity.projection,
            reasons=(
                ProjectionReason(code, message),
                ProjectionReason("game_availability_source", "official:private", applied=False),
            ),
        ),
    )


def test_matching_statuses_group_names_with_local_game_time() -> None:
    state = _state(
        tuple(
            _warning(player, "g1", "confirmed_game_absence", "Out for this game.")
            for player in ("p2", "p1")
        )
    )

    summary = state_availability_summary(state, player_names=NAMES, local_timezone=CHICAGO)

    assert summary == ("• Out: Ann, Ben — Mon 8:00 PM CST.",)
    assert summary == state_availability_summary(
        replace(state, opportunities=tuple(reversed(state.opportunities))),
        player_names=NAMES,
        local_timezone=CHICAGO,
    )
    assert "private" not in "\n".join(summary)


def test_distinct_game_statuses_remain_separate_and_provisional() -> None:
    state = _state(
        (
            _warning("p1", "g1", "confirmed_game_absence", "Out for this game."),
            _warning(
                "p1",
                "g2",
                "availability_uncertainty",
                "Questionable; participation is provisional for this game.",
                hours_later=24,
            ),
        )
    )

    summary = state_availability_summary(state, player_names=NAMES, local_timezone=CHICAGO)

    assert summary == (
        "• Out: Ann — Mon 8:00 PM CST.",
        "• Questionable: Ann — Tue 8:00 PM CST.",
        "• Estimates provisional.",
    )


def test_missing_report_counts_unique_players_across_games() -> None:
    state = _state(
        tuple(
            _warning(
                player, f"g{day}", "availability_uncertainty", MISSING_REPORT, hours_later=24 * day
            )
            for player in ("p1", "p2")
            for day in range(3)
        )
    )

    summary = state_availability_summary(state, player_names=NAMES)

    assert summary == ("• Reports missing for 2 players; estimates provisional.",)
    assert len(state_availability_notes(state, player_names=NAMES)) == 6


def test_unscoped_designations_never_render_as_confirmed_absences() -> None:
    state = _state(
        (
            _warning(
                "p1",
                "g1",
                "availability_uncertainty",
                "An unscoped out designation is available; its game applicability is unverified "
                "and participation remains provisional.",
            ),
        )
    )

    summary = state_availability_summary(state, player_names=NAMES, local_timezone=CHICAGO)

    assert summary == ("• Unverified out: Ann (game applicability unknown).",)


def test_missing_reports_and_unverified_source_are_distinct_warnings() -> None:
    state = _state(
        (
            _warning("p1", "g1", "availability_uncertainty", MISSING_REPORT),
            _warning("p2", "g2", "availability_data_gap", "official_failure:private"),
        )
    )

    assert state_availability_summary(state) == (
        "• Reports missing for 1 player; estimates provisional.",
        "• Availability unverified for 1 player; estimates provisional.",
    )


def test_unknown_uncertainty_keeps_its_explanation_and_player_id() -> None:
    state = _state((_warning("p1", "g1", "availability_uncertainty", "Unverified minutes limit."),))

    summary = state_availability_summary(state)

    assert summary[0] == "• Unverified minutes limit: p1 — Tue 2:00 AM UTC."


def test_past_games_and_missing_projections_do_not_emit_warnings() -> None:
    opportunity = _warning("p1", "g1", "confirmed_game_absence", "Out for this game.")
    state = replace(_state((opportunity,)), decision_time=START)

    assert state_availability_summary(state) == ()
    assert state_availability_summary(_state(())) == ()
    missing = replace(
        opportunity,
        projection=None,
        missing_projection_reason=PlanningReasonCode.MISSING_PROJECTION,
    )
    assert state_availability_summary(_state((missing,))) == ()


def test_player_changes_remain_material_when_summary_counts_match() -> None:
    first = _state((_warning("p1", "g1", "availability_uncertainty", MISSING_REPORT),))
    second = _state((_warning("p2", "g1", "availability_uncertainty", MISSING_REPORT),))

    assert state_availability_summary(first) == state_availability_summary(second)
    assert availability_material_hash("base", state_availability_notes(first)) != (
        availability_material_hash("base", state_availability_notes(second))
    )


def test_trace_and_material_identity_retain_repeated_statuses_for_distinct_games() -> None:
    first = _warning("p1", "g1", "confirmed_game_absence", "Out for this game.")
    second = _warning("p1", "g2", "confirmed_game_absence", "Out for this game.", hours_later=24)
    notes = state_availability_notes(_state((first, second)))

    assert len(notes) == 2
    assert availability_material_hash("base", notes) != availability_material_hash(
        "base", notes[:1]
    )


def test_unscoped_reports_group_each_player_once_and_keep_individual_trace_notes() -> None:
    message = (
        "An unscoped out designation is available; its game applicability is unverified "
        "and participation remains provisional."
    )
    state = _state(
        tuple(
            _warning(player, f"g{day}", "availability_uncertainty", message, hours_later=24 * day)
            for player in ("p2", "p1")
            for day in range(3)
        )
        + (_warning("p1", "confirmed", "confirmed_game_absence", "Out for this game."),)
    )

    summary = state_availability_summary(state, player_names=NAMES, local_timezone=CHICAGO)

    assert summary == (
        "• Out: Ann — Mon 8:00 PM CST.",
        "• Unverified out: Ann, Ben (game applicability unknown).",
    )
    assert summary == state_availability_summary(
        replace(state, opportunities=tuple(reversed(state.opportunities))),
        player_names=NAMES,
        local_timezone=CHICAGO,
    )
    assert len(state_availability_notes(state, player_names=NAMES)) == 7
