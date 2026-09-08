"""A supplied report must be latest, attributable and unambiguous before it can support a proxy."""

from dataclasses import replace
from pathlib import Path

import pytest
from injury_team_evidence_support import nba_fixture, report_cache, report_ledger

from sleeper_manager.backtesting.replay.injury_team_evidence import load_injury_team_evidence


def test_report_at_tipoff_is_approximate_and_only_links_the_correct_game(tmp_path: Path) -> None:
    inputs = nba_fixture()
    evidence = load_injury_team_evidence(
        report_ledger(tmp_path, [report_cache(tmp_path, "at-tipoff", hour=20)]), inputs
    )
    assert len(evidence.observations) == 1
    observation = evidence.observations[0]
    assert observation.observed_at == inputs.games[-1].start_time
    assert observation.provider_player_id == "provider-1"
    assert observation.team_id == "CHI"
    assert observation.approximate
    assert len(evidence.source_fingerprints) == 3


@pytest.mark.parametrize("mode", ["omitted", "pending", "unsubmitted", "tied"])
def test_newer_or_tied_unsupported_report_cannot_reuse_older_entry(
    tmp_path: Path, mode: str
) -> None:
    older = report_cache(tmp_path, "older", hour=18)
    newer = report_cache(
        tmp_path,
        "newer",
        hour=18 if mode == "tied" else 19,
        included=mode not in {"omitted", "tied"},
        pending=mode == "pending",
        submitted=mode != "unsubmitted",
    )
    for paths in ([older, newer], [newer, older]):
        assert (
            load_injury_team_evidence(report_ledger(tmp_path, paths), nba_fixture()).observations
            == ()
        )


def test_after_tipoff_report_cannot_revise_the_proxy(tmp_path: Path) -> None:
    before = report_cache(tmp_path, "before")
    after = report_cache(tmp_path, "after", hour=21, included=False)
    evidence = load_injury_team_evidence(report_ledger(tmp_path, [before, after]), nba_fixture())
    assert len(evidence.observations) == 1
    assert (
        load_injury_team_evidence(report_ledger(tmp_path, [after]), nba_fixture()).observations
        == ()
    )


def test_ambiguous_provider_names_and_schedule_remain_unresolved(tmp_path: Path) -> None:
    inputs = nba_fixture()
    ledger = report_ledger(tmp_path, [report_cache(tmp_path, "report")])
    duplicate = replace(inputs.provider_players[0], provider_id="other")
    assert not load_injury_team_evidence(
        ledger, replace(inputs, provider_players=(*inputs.provider_players, duplicate))
    ).observations
    assert not load_injury_team_evidence(
        ledger,
        replace(inputs, games=(*inputs.games, replace(inputs.games[-1], provider_id="other"))),
    ).observations


def test_changed_pdf_and_changed_cache_are_rejected(tmp_path: Path) -> None:
    for extension in (".pdf", ".json"):
        report = report_cache(tmp_path, "changed")
        ledger = report_ledger(tmp_path, [report])
        with report.with_suffix(extension).open("ab") as file:
            file.write(b" ")
        with pytest.raises(RuntimeError, match="hash mismatch"):
            load_injury_team_evidence(ledger, nba_fixture())


def test_agreeing_tied_reports_keep_a_proxy_in_both_orders(tmp_path: Path) -> None:
    first, second = report_cache(tmp_path, "first"), report_cache(tmp_path, "second")
    a = load_injury_team_evidence(report_ledger(tmp_path, [first, second]), nba_fixture())
    b = load_injury_team_evidence(report_ledger(tmp_path, [second, first]), nba_fixture())
    assert a.observations == b.observations
    assert len(a.observations) == 1


def test_bundle_carries_proxy_provenance_and_preserves_missing_outcomes(tmp_path: Path) -> None:
    from datetime import date

    from team_week_source_support import LEAGUE_ID, RETRIEVED_AT, _write_archive

    from sleeper_manager.backtesting.replay.team_week_bundle import (
        HistoricalTeamWeekBundleError,
        HistoricalTeamWeekBundleRequest,
        bootstrap_historical_team_week_bundle,
    )

    _write_archive(tmp_path)
    ledger = report_ledger(tmp_path, [report_cache(tmp_path, "target")])
    inputs = nba_fixture()
    request = HistoricalTeamWeekBundleRequest(LEAGUE_ID, 4, 16, date(2026, 2, 2))
    output = bootstrap_historical_team_week_bundle(
        tmp_path, request, nba_inputs=inputs, injury_team_evidence_path=ledger, now=RETRIEVED_AT
    )
    assert output.team_week.coverage.inferred_team_membership == 1
    assert output.team_week.coverage.joined_player_games == 1
    assert not output.team_week.complete
    assert any(
        row.name == "injury-team-proxy-ledger" for row in output.manifest.source_fingerprints
    )
    without_outcome = replace(
        inputs,
        player_box_scores=tuple(box for box in inputs.player_box_scores if box.game_id != "target"),
    )
    with pytest.raises(HistoricalTeamWeekBundleError, match="no joined"):
        bootstrap_historical_team_week_bundle(
            tmp_path,
            request,
            nba_inputs=without_outcome,
            injury_team_evidence_path=ledger,
            now=RETRIEVED_AT,
        )
