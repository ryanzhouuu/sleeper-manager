"""Official statuses must survive identity, scope, freshness, and submission checks."""

from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.domain.nba import AvailabilityStatus, ProviderPlayer, SourceMetadata
from sleeper_manager.integrations.nba.historical_feature_models import AvailabilityObservation
from sleeper_manager.integrations.nba.official_injury_models import (
    EASTERN_TIME,
    OfficialInjuryReportEntry,
    OfficialInjuryReportSnapshot,
    OfficialTeamReportStatus,
    ReportSubmissionStatus,
)
from sleeper_manager.workflows.game_availability import bind_official_game_availability
from tests.sleeper_manager.workflows.planning_collection_support import NOW, _game


def evidence(*, status: AvailabilityStatus = AvailabilityStatus.OUT):  # type: ignore[no-untyped-def]
    game = _game("g1")
    local = game.start_time.astimezone(EASTERN_TIME)
    source = SourceMetadata("official", "report", NOW, NOW, content_hash="sha256-evidence")
    player = ProviderPlayer("401", "Point Guard One", game.home_team_id, "CHI", True, source)
    entry = OfficialInjuryReportEntry(
        local.date(),
        local.time().replace(tzinfo=None),
        "BOS@CHI",
        "CHI",
        "Chicago Bulls",
        "One, Point Guard",
        status,
        "injury",
    )
    team = OfficialTeamReportStatus(
        entry.game_date,
        entry.game_time,
        entry.matchup,
        "CHI",
        entry.team_name,
        ReportSubmissionStatus.SUBMITTED,
    )
    return game, player, OfficialInjuryReportSnapshot(NOW, (entry,), (team,), source)


def bind(snapshot, player, *games):  # type: ignore[no-untyped-def]
    return bind_official_game_availability(
        snapshot, players=(player,), games=games, cutoff=NOW, max_age=timedelta(minutes=15)
    )


def test_out_is_confirmed_only_for_matching_game() -> None:
    game, player, snapshot = evidence()
    future = replace(game, provider_id="later", start_time=game.start_time + timedelta(days=2))
    result = bind(snapshot, player, game, future)
    assert len(result.assignments) == 1
    provider, report = result.assignments[0]
    assert provider == "401"
    assert report.game_id == "g1"
    assert report.confirmed_absence
    assert report.observed_at == NOW
    assert "sha256-evidence" in report.source_version


@pytest.mark.parametrize(
    "status",
    [
        AvailabilityStatus.QUESTIONABLE,
        AvailabilityStatus.PROBABLE,
        AvailabilityStatus.DOUBTFUL,
        AvailabilityStatus.AVAILABLE,
    ],
)
def test_uncertain_or_available_status_is_not_confirmed_absence(status: AvailabilityStatus) -> None:
    game, player, snapshot = evidence(status=status)
    report = bind(snapshot, player, game).assignments[0][1]
    assert report.status is status
    assert not report.confirmed_absence


@pytest.mark.parametrize(
    "kind", ["stale", "stale_publication", "future_retrieval", "future_publication", "hash"]
)
def test_unusable_evidence_does_not_assign_status(kind: str) -> None:
    game, player, snapshot = evidence()
    if kind == "stale":
        snapshot = replace(
            snapshot, source=replace(snapshot.source, retrieved_at=NOW - timedelta(minutes=16))
        )
    elif kind == "stale_publication":
        snapshot = replace(snapshot, published_at=NOW - timedelta(hours=1, seconds=1))
    elif kind == "future_retrieval":
        snapshot = replace(
            snapshot, source=replace(snapshot.source, retrieved_at=NOW + timedelta(seconds=1))
        )
    elif kind == "future_publication":
        snapshot = replace(snapshot, published_at=NOW + timedelta(seconds=1))
    else:
        snapshot = replace(snapshot, source=replace(snapshot.source, content_hash=None))
    result = bind(snapshot, player, game)
    assert result.assignments == ()
    assert result.warnings


def test_missing_submission_and_not_listed_remain_distinct() -> None:
    game, player, snapshot = evidence()
    snapshot = replace(snapshot, entries=())
    unlisted = bind(snapshot, player, game).assignments[0][1]
    missing = bind(
        replace(
            snapshot,
            team_statuses=(
                replace(snapshot.team_statuses[0], status=ReportSubmissionStatus.NOT_YET_SUBMITTED),
            ),
        ),
        player,
        game,
    ).assignments[0][1]
    assert unlisted.observation is AvailabilityObservation.NOT_LISTED
    assert missing.observation is AvailabilityObservation.MISSING_REPORT
    assert unlisted.status is missing.status is AvailabilityStatus.UNKNOWN


def test_ambiguous_rows_and_mismatched_team_never_confirm_absence() -> None:
    game, player, snapshot = evidence()
    duplicate = replace(snapshot, team_statuses=snapshot.team_statuses * 2)
    assert bind(duplicate, player, game).assignments == ()
    other_team = replace(snapshot, entries=(replace(snapshot.entries[0], team_abbreviation="BOS"),))
    result = bind(other_team, player, game)
    assert not any(report.confirmed_absence for _, report in result.assignments)


def test_late_update_replaces_previous_out_without_carrying_confirmation() -> None:
    game, player, snapshot = evidence()
    old = bind(snapshot, player, game).assignments[0][1]
    update = replace(
        snapshot, entries=(replace(snapshot.entries[0], status=AvailabilityStatus.AVAILABLE),)
    )
    new = bind(update, player, game).assignments[0][1]
    assert old.confirmed_absence
    assert not new.confirmed_absence


def test_official_twelve_hour_clock_binds_only_to_unique_scheduled_game() -> None:
    game, player, snapshot = evidence()
    game = replace(game, start_time=game.start_time + timedelta(hours=4))
    entry = replace(snapshot.entries[0], game_time=snapshot.entries[0].game_time.replace(hour=7))
    team = replace(snapshot.team_statuses[0], game_time=entry.game_time)
    snapshot = replace(snapshot, entries=(entry,), team_statuses=(team,))
    assert game.start_time.astimezone(EASTERN_TIME).hour == 19
    assert bind(snapshot, player, game).assignments[0][1].confirmed_absence
    ambiguous = replace(
        game, provider_id="morning", start_time=game.start_time - timedelta(hours=12)
    )
    assert bind(snapshot, player, game, ambiguous).assignments == ()
