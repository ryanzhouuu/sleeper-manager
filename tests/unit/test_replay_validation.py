from sleeper_manager.backtesting.replay_models import TeamWeekComparison
from sleeper_manager.backtesting.replay_report import markdown_report, report_payload
from sleeper_manager.backtesting.replay_validation import (
    PolicyScore,
    paired_policy_delta,
    summarize_team_weeks,
    summarize_with_exclusions,
)


def comparison(oracle: float, model: float) -> TeamWeekComparison:
    return TeamWeekComparison(
        oracle,
        model,
        oracle - model,
        model / oracle if oracle else None,
        (("nonnegative_regret", True),),
    )


def test_team_week_summary_reports_primary_regret_and_capture() -> None:
    summary = summarize_with_exclusions(
        [comparison(100, 90), comparison(0, 0)],
        ["missing_eligibility"],
        starter_slot_count=2,
        bootstrap_samples=25,
    )

    assert summary.team_week_count == 2
    assert summary.mean_regret == 5
    assert summary.mean_score_capture == 0.9
    assert summary.regret_per_starter_slot == 2.5
    assert summary.excluded_team_weeks == 1
    assert summary.mean_regret_interval is not None


def test_paired_deltas_use_only_complete_common_team_weeks_and_report() -> None:
    delta = paired_policy_delta(
        [PolicyScore("a", 12, 20), PolicyScore("b", 8, 10, complete=False)],
        [PolicyScore("a", 10, 20), PolicyScore("c", 5, 10)],
        samples=25,
    )
    payload = report_payload(
        league_id="league",
        season="2025",
        oracle_label="best_known_constraints_oracle",
        summary=summarize_team_weeks([comparison(20, 12)], bootstrap_samples=10),
    )

    assert delta.common_team_week_count == 1
    assert delta.mean_candidate_minus_baseline == 2
    assert "Mean model-policy regret" in markdown_report((payload,))
