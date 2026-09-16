from datetime import UTC, datetime, timedelta

from sleeper_manager.backtesting.cohorts import (
    IndependentCohortRanker,
    cohort_for_rank,
    rank_players_as_of,
    ranked_players_as_of,
)
from sleeper_manager.domain.nba import AvailabilityStatus
from sleeper_manager.domain.scoring import BoxScoreLine, ScoringPolicy
from sleeper_manager.integrations.nba.historical_feature_dataset import HistoricalFeatureRow
from sleeper_manager.integrations.nba.historical_feature_models import AvailabilityObservation

POINTS_POLICY = ScoringPolicy(points=1)
REBOUNDS_POLICY = ScoringPolicy(rebounds=1)


def make_row(
    player_id: str,
    start: datetime,
    points: int = 0,
    *,
    rebounds: int = 0,
    minutes: float = 30.0,
    did_play: bool = True,
    status: AvailabilityStatus = AvailabilityStatus.AVAILABLE,
) -> HistoricalFeatureRow:
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
        availability_status=status,
        availability_observation=AvailabilityObservation.MISSING_REPORT,
        availability_detail=None,
        availability_observed_at=None,
        prior_games=0,
        prior_minutes_mean=None,
        prior_minutes_last=None,
        prior_start_rate=None,
        target_minutes=minutes if did_play else None,
        target_started=did_play,
        target_did_play=did_play,
        target_box_score=BoxScoreLine(points=points, rebounds=rebounds),
        target_line_points=points,
        target_line_rebounds=rebounds,
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
    ranks = rank_players_as_of(rows, as_of, scoring_policy=POINTS_POLICY)

    assert ranks == {"a": 1, "b": 2}
    assert cohort_for_rank(108) == "top_108"
    assert cohort_for_rank(109) == "ranks_109_180"
    assert cohort_for_rank(181) == "below_180"


def test_missing_player_is_ranked_without_future_observations() -> None:
    as_of = datetime(2026, 1, 10, tzinfo=UTC)
    ranks = rank_players_as_of([], as_of, player_ids=["p2", "p1"], scoring_policy=POINTS_POLICY)
    assert ranks == {"p1": 1, "p2": 2}


def test_scoring_weights_change_ranks_in_the_expected_direction() -> None:
    as_of = datetime(2026, 1, 10, tzinfo=UTC)
    prior = as_of - timedelta(days=1)
    # "scorer" racks up points but no rebounds; "rebounder" racks up rebounds but no points.
    rows = [
        make_row("scorer", prior, points=40, rebounds=0),
        make_row("rebounder", prior, points=0, rebounds=40),
    ]

    points_ranks = rank_players_as_of(rows, as_of, scoring_policy=POINTS_POLICY)
    rebounds_ranks = rank_players_as_of(rows, as_of, scoring_policy=REBOUNDS_POLICY)

    assert points_ranks["scorer"] < points_ranks["rebounder"]
    assert rebounds_ranks["rebounder"] < rebounds_ranks["scorer"]


def test_cohort_boundaries_108_109_180_181_are_exact() -> None:
    as_of = datetime(2026, 1, 10, tzinfo=UTC)
    prior = as_of - timedelta(days=1)
    # 181 players with strictly decreasing points -> deterministic ranks 1..181.
    rows = [make_row(f"p{i:03d}", prior, points=1000 - i) for i in range(181)]

    ranked = ranked_players_as_of(rows, as_of, scoring_policy=POINTS_POLICY)
    by_rank = {player.rank: player for player in ranked}

    assert by_rank[108].cohort == "top_108"
    assert by_rank[108].top_180 is True
    assert by_rank[109].cohort == "ranks_109_180"
    assert by_rank[109].top_180 is True
    assert by_rank[180].cohort == "ranks_109_180"
    assert by_rank[180].top_180 is True
    assert by_rank[181].cohort == "below_180"
    assert by_rank[181].top_180 is False


def test_ties_missing_history_and_early_season_fallback_are_deterministic() -> None:
    as_of = datetime(2025, 11, 5, tzinfo=UTC)  # early in the 2025-26 season
    prior = as_of - timedelta(days=1)

    # Ties: identical score, tie-broken by player_id.
    tied_rows = [make_row("z-tie", prior, points=10), make_row("a-tie", prior, points=10)]
    tied_ranks = rank_players_as_of(tied_rows, as_of, scoring_policy=POINTS_POLICY)
    assert tied_ranks["a-tie"] < tied_ranks["z-tie"]

    # Missing history: a rookie with no rows at all, entered only via player_ids, scores 0.0
    # and is deterministic across repeated calls.
    rookie_ranks_1 = rank_players_as_of(
        [], as_of, player_ids=["rookie"], scoring_policy=POINTS_POLICY
    )
    rookie_ranks_2 = rank_players_as_of(
        [], as_of, player_ids=["rookie"], scoring_policy=POINTS_POLICY
    )
    assert rookie_ranks_1 == rookie_ranks_2 == {"rookie": 1}

    # Early-season fallback: a veteran with only prior-season evidence (no current-season rows)
    # ranks well above a true rookie with the same population entry (via player_ids), and is
    # deterministic, because the fallback pulls in the player's own established prior-season
    # rate/minutes instead of the generic league-average constants.
    prior_season_game = datetime(2025, 3, 1, tzinfo=UTC)  # 2024-25 season
    veteran_rows = [make_row("veteran", prior_season_game, points=50, minutes=25.0)]
    ranked_1 = ranked_players_as_of(
        veteran_rows, as_of, player_ids=["rookie", "veteran"], scoring_policy=POINTS_POLICY
    )
    ranked_2 = ranked_players_as_of(
        veteran_rows, as_of, player_ids=["rookie", "veteran"], scoring_policy=POINTS_POLICY
    )
    scores_1 = {player.player_id: player.baseline_score for player in ranked_1}
    scores_2 = {player.player_id: player.baseline_score for player in ranked_2}
    assert scores_1 == scores_2  # deterministic
    assert scores_1["veteran"] > scores_1["rookie"]
    # Generic league-average constants alone (24.0 minutes * 0.5 rate, no availability discount
    # applied) would produce ~12.0; the veteran's own prior-season rate (50 pts / 25 min = 2.0)
    # should pull the fallback score well above that generic level.
    assert scores_1["veteran"] > 20.0
    assert scores_1["rookie"] == 0.0


def test_players_known_only_from_future_or_other_seasons_do_not_enter_earlier_population() -> None:
    as_of = datetime(2025, 11, 5, tzinfo=UTC)  # early in the 2025-26 season

    # A player whose only row is strictly after as_of never enters the population.
    future_only = make_row("future-only", as_of + timedelta(days=1), points=1000)
    # A player whose only row is in a prior season (not the current one) and who is not in the
    # current batch never enters the population either -- unlike a bare "any prior row" rule.
    prior_season_only = make_row("prior-season-only", datetime(2025, 3, 1, tzinfo=UTC), points=1000)
    current_season_player = make_row("current", as_of - timedelta(days=1), points=10)

    ranks = rank_players_as_of(
        [future_only, prior_season_only, current_season_player],
        as_of,
        scoring_policy=POINTS_POLICY,
    )

    assert "future-only" not in ranks
    assert "prior-season-only" not in ranks
    assert ranks == {"current": 1}


def test_same_tipoff_and_future_outcomes_cannot_change_earlier_ranks() -> None:
    tipoff = datetime(2025, 11, 5, tzinfo=UTC)
    established = tipoff - timedelta(days=3)
    rows = [
        make_row("a", established, points=10),
        # Same-tipoff rows for both players -- these must never be observable at as_of=tipoff.
        make_row("a", tipoff, points=999),
        make_row("b", tipoff, points=999),
    ]

    ranked_without_b_in_batch = ranked_players_as_of(rows, tipoff, scoring_policy=POINTS_POLICY)
    scores_without_b = {
        player.player_id: player.baseline_score for player in ranked_without_b_in_batch
    }
    assert "b" not in scores_without_b
    assert {player.player_id: player.rank for player in ranked_without_b_in_batch} == {"a": 1}

    ranked_with_b_in_batch = ranked_players_as_of(
        rows, tipoff, player_ids=["b"], scoring_policy=POINTS_POLICY
    )
    scores_with_b = {player.player_id: player.baseline_score for player in ranked_with_b_in_batch}
    # b enters the population via the current batch, but its same-tipoff row must not count as
    # prior evidence -- b has no observable history, so it scores exactly like a rookie.
    assert scores_with_b["b"] == 0.0
    # a's score is unaffected by whether b is present in the batch.
    assert scores_with_b["a"] == scores_without_b["a"]


def test_candidate_ordering_cannot_affect_rank_maps() -> None:
    as_of = datetime(2026, 1, 10, tzinfo=UTC)
    prior = as_of - timedelta(days=1)
    forward = [make_row(f"p{i}", prior, points=10 + i) for i in range(20)]
    shuffled = list(reversed(forward[1::2])) + list(forward[0::2])

    forward_ranks = rank_players_as_of(forward, as_of, scoring_policy=POINTS_POLICY)
    shuffled_ranks = rank_players_as_of(shuffled, as_of, scoring_policy=POINTS_POLICY)

    assert forward_ranks == shuffled_ranks


def test_incremental_ranker_output_equals_a_simple_reference_ranker() -> None:
    base = datetime(2025, 10, 20, tzinfo=UTC)
    rows: list[HistoricalFeatureRow] = []
    for day in range(20):
        timestamp = base + timedelta(days=day)
        for player_index in range(5):
            player_id = f"player-{player_index}"
            rows.append(make_row(player_id, timestamp, points=10 + player_index * 3 + day))
    ordered_rows = tuple(sorted(rows, key=lambda row: (row.game_start, row.player_id)))
    cutoffs = sorted({row.game_start for row in ordered_rows})
    # Include one cutoff strictly after the last row to exercise the fully-populated case too.
    cutoffs.append(cutoffs[-1] + timedelta(days=1))

    ranker = IndependentCohortRanker()
    for as_of in cutoffs:
        incremental = ranker.rank_players_as_of(
            ordered_rows, as_of, scoring_policy=POINTS_POLICY, dataset_version="ds-incremental"
        )
        reference = rank_players_as_of(ordered_rows, as_of, scoring_policy=POINTS_POLICY)
        assert incremental == reference, f"mismatch at {as_of.isoformat()}"


def test_incompatible_dataset_or_scoring_policy_change_does_not_reuse_a_stale_index() -> None:
    as_of = datetime(2026, 1, 10, tzinfo=UTC)
    prior = as_of - timedelta(days=1)
    rows_a = (make_row("only-in-a", prior, points=10),)
    rows_b = (make_row("only-in-b", prior, points=999, rebounds=999),)

    ranker = IndependentCohortRanker()
    ranks_a = ranker.rank_players_as_of(
        rows_a, as_of, scoring_policy=POINTS_POLICY, dataset_version="ds-a"
    )
    ranks_b = ranker.rank_players_as_of(
        rows_b, as_of, scoring_policy=REBOUNDS_POLICY, dataset_version="ds-b"
    )

    assert ranks_a == {"only-in-a": 1}
    assert ranks_b == {"only-in-b": 1}
    assert "only-in-a" not in ranks_b


def test_incremental_ranker_rejects_chronological_regression() -> None:
    base = datetime(2026, 1, 10, tzinfo=UTC)
    rows = tuple(
        make_row("player", base + timedelta(days=day), points=10 + day) for day in range(5)
    )

    ranker = IndependentCohortRanker()
    ranker.rank_players_as_of(rows, base + timedelta(days=4), scoring_policy=POINTS_POLICY)
    try:
        ranker.rank_players_as_of(rows, base + timedelta(days=1), scoring_policy=POINTS_POLICY)
    except Exception as error:  # noqa: BLE001 - asserting the specific error type below
        from sleeper_manager.backtesting.cohorts import CohortRankingError

        assert isinstance(error, CohortRankingError)
    else:
        raise AssertionError("expected a chronological regression to raise explicitly")
