"""Independent opportunity accounting, source conflicts and deterministic history export."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from sleeper_manager.backtesting.artifacts import canonical_json
from sleeper_manager.backtesting.replay.inputs.models import GameRosterCensus, SourceFingerprint
from sleeper_manager.domain.scoring import ScoringPolicy
from sleeper_manager.projections.hybrid_config import HybridProjectionConfig, HybridProjectionError
from sleeper_manager.projections.hybrid_history_adapter import (
    build_hybrid_history,
    write_hybrid_history_build,
)
from sleeper_manager.projections.hybrid_history_artifact import load_hybrid_history_artifact
from sleeper_manager.projections.hybrid_history_rows import (
    HistoryFinalization,
    HistoryOutcome,
    parse_hybrid_history_rows,
)
from sleeper_manager.projections.hybrid_participation import HybridParticipation
from sleeper_manager.projections.hybrid_pools import HybridHistoryPools
from tests.sleeper_manager.projections.test_hybrid_history_rows import FINAL, GAME, TIMING, raw_row

CENSUS = GameRosterCensus(
    "100",
    "1",
    "2",
    ("200", "201", "202"),
    (("200", "1"), ("201", "2")),
    "reviewed-final-report#page=1",
)
CENSUS_SOURCE = SourceFingerprint("reviewed-census", "a" * 64, "v1")
DNP = HistoryOutcome("201", "100", "2", False, "reviewed-inactive-sha256")


def build(rows=None, **changes):  # type: ignore[no-untyped-def]
    sources = (
        parse_hybrid_history_rows(
            [raw_row()] if rows is None else rows,
            games=(GAME,),
            resolved_player_ids=("200", "201", "202"),
            finalization=TIMING,
            source_version="raw-sha256",
        ),
    )
    kwargs = dict(
        games=(GAME,),
        resolved_player_ids=("200", "201", "202"),
        finalization=TIMING,
        censuses=(CENSUS,),
        census_sources=(CENSUS_SOURCE,),
        supplemental_dnp=(DNP,),
    )
    kwargs.update(changes)
    return build_hybrid_history(kwargs.pop("sources", sources), **kwargs)


def test_census_joins_explicit_played_and_dnp_but_absent_player_is_not_dnp() -> None:
    result = build()
    assert [(row.player_id, row.did_play) for row in result.history.opportunities] == [
        ("200", True),
        ("201", False),
    ]
    assert [row.player_id for row in result.history.observations] == ["200"]
    assert result.expected_opportunities == 2
    assert result.reviewed_player_games == 3
    assert result.uncensused_outcomes == 0


def test_absent_outcome_stays_a_gap_even_with_complete_roster_review() -> None:
    result = build(supplemental_dnp=())
    assert [row.player_id for row in result.history.opportunities] == ["200"]
    assert result.expected_opportunities == 2
    assert any(gap.player_id == "201" and gap.reason == "missing_outcome" for gap in result.gaps)


def test_uncensused_boxes_never_expand_participation_denominator() -> None:
    result = build(censuses=(), census_sources=())
    assert len(result.history.observations) == 1
    assert not result.history.opportunities
    assert result.uncensused_outcomes == 2


def test_agreeing_raw_dnp_and_official_supplement_bind_both_sources() -> None:
    rows = [raw_row(), raw_row(athlete_id=201, team_id=2, did_not_play=True)]
    with_supplement = build(rows)
    without = build(rows, supplemental_dnp=())
    assert len(with_supplement.history.opportunities) == 2
    assert (
        with_supplement.history.opportunities[1].source_version
        != without.history.opportunities[1].source_version
    )


def test_missing_timing_is_visible_without_manufactured_opportunities() -> None:
    parsed = parse_hybrid_history_rows(
        [raw_row()],
        games=(GAME,),
        resolved_player_ids=("200",),
        finalization={},
        source_version="raw-sha256",
    )
    result = build(sources=(parsed,), finalization={})
    assert not result.history.opportunities and not result.history.observations
    assert (
        sum(gap.player_id == "200" and gap.reason == "missing_finalization" for gap in result.gaps)
        == 1
    )


def test_unresolved_census_identity_is_a_gap_and_remains_in_expected_count() -> None:
    result = build(resolved_player_ids=("200", "202"), supplemental_dnp=())
    assert result.expected_opportunities == 2
    assert any(gap.player_id == "201" and gap.reason == "unresolved_player" for gap in result.gaps)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"supplemental_dnp": (replace(DNP, player_id="200"),)}, "conflicting_history_outcome"),
        ({"supplemental_dnp": (replace(DNP, did_play=True),)}, "supplement_requires_explicit_dnp"),
        ({"supplemental_dnp": (DNP, DNP)}, "duplicate_supplemental_outcome"),
        ({"supplemental_dnp": (replace(DNP, team_id="3"),)}, "outcome_team_mismatch"),
        ({"supplemental_dnp": (replace(DNP, game_id="other"),)}, "outcome_without_final_game"),
        ({"resolved_player_ids": ("200",)}, "unresolved_outcome_identity"),
        ({"censuses": (CENSUS, CENSUS)}, "duplicate_history_census"),
        (
            {"censuses": (replace(CENSUS, home_team_id="2", away_team_id="1"),)},
            "census_game_mismatch",
        ),
        (
            {"censuses": (replace(CENSUS, player_teams=(("200", "2"), ("201", "2"))),)},
            "census_outcome_mismatch",
        ),
        ({"censuses": (replace(CENSUS, player_teams=(("201", "2"),)),)}, "census_outcome_mismatch"),
        ({"census_sources": ()}, "missing_census_sources"),
    ],
)
def test_incompatible_evidence_is_rejected(change: dict, reason: str) -> None:  # type: ignore[type-arg]
    with pytest.raises(HybridProjectionError, match=reason):
        build(**change)


def test_overlapping_source_rows_and_changed_finalization_are_rejected() -> None:
    parsed = parse_hybrid_history_rows(
        [raw_row()],
        games=(GAME,),
        resolved_player_ids=("200",),
        finalization=TIMING,
        source_version="v1",
    )
    with pytest.raises(HybridProjectionError, match="duplicate_history_source"):
        build(sources=(parsed, parsed))
    with pytest.raises(HybridProjectionError, match="duplicate_history_outcome"):
        build(sources=(parsed, replace(parsed, source_version="v2")))
    with pytest.raises(HybridProjectionError, match="history_observation_mismatch"):
        build(finalization={"100": HistoryFinalization(FINAL + timedelta(seconds=1), "v1")})


def test_source_and_cohort_changes_alter_identity_but_order_does_not() -> None:
    result = build()
    reordered = replace(
        CENSUS,
        reviewed_player_ids=tuple(reversed(CENSUS.reviewed_player_ids)),
        player_teams=tuple(reversed(CENSUS.player_teams)),
    )
    assert build(censuses=(reordered,)) == result
    assert (
        build(
            census_sources=(replace(CENSUS_SOURCE, content_hash="b" * 64),)
        ).history.dataset_version
        != result.history.dataset_version
    )
    assert (
        build(
            censuses=(replace(CENSUS, reviewed_player_ids=("200", "201")),)
        ).history.dataset_version
        != result.history.dataset_version
    )
    assert (
        build(
            supplemental_dnp=(replace(DNP, source_version="another-proof"),)
        ).history.dataset_version
        != result.history.dataset_version
    )
    uncensused = build([], censuses=(), census_sources=())
    empty = build([], censuses=(), census_sources=(), supplemental_dnp=())
    assert uncensused.history.dataset_version != empty.history.dataset_version


def test_export_round_trips_and_refuses_conflicting_existing_artifacts(tmp_path: Path) -> None:
    result = build()
    write_hybrid_history_build(tmp_path, result)
    assert load_hybrid_history_artifact(tmp_path / "history.json") == result.history
    coverage = (tmp_path / "coverage.json").read_bytes()
    history = (tmp_path / "history.json").read_bytes()
    modified = (tmp_path / "history.json").stat().st_mtime_ns
    assert coverage == canonical_json(result.coverage).encode()
    write_hybrid_history_build(tmp_path, result)
    assert (tmp_path / "history.json").stat().st_mtime_ns == modified
    with pytest.raises(HybridProjectionError, match="history_output_conflict"):
        write_hybrid_history_build(tmp_path, build(supplemental_dnp=()))
    assert (tmp_path / "history.json").read_bytes() == history
    assert (tmp_path / "coverage.json").read_bytes() == coverage


def test_model_consumers_enforce_cutoff_and_missing_foul_coverage() -> None:
    data = build().history
    before = FINAL - timedelta(seconds=1)
    after = FINAL + timedelta(seconds=1)
    config = HybridProjectionConfig()
    participation = HybridParticipation(data, cutoff=after, game_start=after, config=config)
    assert participation.estimate("200", game_id="next").pooled_count == 1
    assert not HybridParticipation(
        data, cutoff=before, game_start=after, config=config
    ).opportunities
    pools = HybridHistoryPools(
        data, cutoff=after, game_start=after, scoring_policy=ScoringPolicy(points=1), config=config
    )
    assert len(pools.games["200"]) == 1
    fouls = HybridHistoryPools(
        data,
        cutoff=after,
        game_start=after,
        scoring_policy=ScoringPolicy(points=1, technical_foul=-1),
        config=config,
    )
    assert fouls.excluded_games == [("200", "100")]
    assert not HybridHistoryPools(
        data, cutoff=before, game_start=after, scoring_policy=ScoringPolicy(points=1), config=config
    ).games


def test_export_checks_coverage_conflicts_before_creating_history(tmp_path: Path) -> None:
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_bytes(b"existing evidence")
    with pytest.raises(HybridProjectionError, match="history_output_conflict"):
        write_hybrid_history_build(tmp_path, build())
    assert not (tmp_path / "history.json").exists()
    assert coverage_path.read_bytes() == b"existing evidence"
