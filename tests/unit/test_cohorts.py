from datetime import UTC, datetime, timedelta

from sleeper_manager.backtesting.cohorts import cohort_for_rank, rank_players_as_of
from sleeper_manager.domain.nba import AvailabilityStatus
from sleeper_manager.domain.scoring import BoxScoreLine
from sleeper_manager.integrations.nba.historical_features import (
    AvailabilityObservation,
    HistoricalFeatureRow,
)


def make_row(player_id: str, start: datetime, points: int) -> HistoricalFeatureRow:
    return HistoricalFeatureRow(
        dataset_version="fixture",
        available_as_of=start,
        player_id=player_id,
        sleeper_id=None,
        game_id=f"{player_id}-{start.isoformat()}",
        game_start=start,
        team_id="team",
        opponent_team_id="opponent",
        opponent_abbreviation="opp",
        is_home=True,
        days_rest=1,
        is_back_to_back=False,
        availability_status=AvailabilityStatus.AVAILABLE,
        availability_observation=AvailabilityObservation.MISSING_REPORT,
        availability_detail=None,
        availability_observed_at=None,
        prior_games=0,
        prior_minutes_mean=None,
        prior_minutes_last=None,
        prior_start_rate=None,
        target_minutes=30,
        target_started=True,
        target_did_play=True,
        target_box_score=BoxScoreLine(points=points),
        target_line_points=points,
        target_line_rebounds=0,
        target_line_assists=0,
        target_line_steals=0,
        target_line_blocks=0,
        target_line_turnovers=0,
        source_lineage=(),
    )


def test_ranker_is_prior_only_and_tie_breaks_by_player_id() -> None:
    as_of = datetime(2026, 1, 10, tzinfo=UTC)
    rows = [
        make_row("b", as_of - timedelta(days=1), 10),
        make_row("a", as_of - timedelta(days=1), 10),
        make_row("future-star", as_of + timedelta(days=1), 1000),
    ]
    ranks = rank_players_as_of(rows, as_of)

    assert ranks == {"a": 1, "b": 2}
    assert cohort_for_rank(108) == "top_108"
    assert cohort_for_rank(109) == "ranks_109_180"
    assert cohort_for_rank(181) == "below_180"


def test_missing_player_is_ranked_without_future_observations() -> None:
    as_of = datetime(2026, 1, 10, tzinfo=UTC)
    ranks = rank_players_as_of([], as_of, player_ids=["p2", "p1"])
    assert ranks == {"p1": 1, "p2": 2}
