"""Exercise the candidate against real SQLite archive selection and receipt freshness."""

from dataclasses import replace
from datetime import timedelta

import pytest

from sleeper_manager.domain.forecast_capture import ForecastCaptureOutcome, ForecastCoverage
from sleeper_manager.domain.forecast_revision_identity import forecast_semantic_hash
from sleeper_manager.persistence.forecast_repository import ForecastCaptureWrite
from sleeper_manager.persistence.forecast_sqlite import SQLiteForecastArchiveRepository
from sleeper_manager.projections.hybrid_config import HybridProjectionConfig, HybridProjectionError
from sleeper_manager.projections.hybrid_provider import HybridProjectionProvider
from tests.sleeper_manager.persistence.forecast_sqlite_support import (
    SOURCE,
    failed_capture,
    successful_capture,
)
from tests.sleeper_manager.projections.hybrid_support import CUTOFF, history
from tests.sleeper_manager.projections.test_hybrid_model import POLICY, TARGET, batch, forecast


def capture(receipt: str, at, *, outcome=ForecastCaptureOutcome.CHANGED, player=None):  # type: ignore[no-untyped-def]
    old = successful_capture(
        receipt_id=receipt, persisted_at=at, player_id=TARGET.sleeper_player_id
    )
    record = player or forecast(batch().pools.pool("target").means).forecast
    semantic = forecast_semantic_hash(SOURCE, (record,))
    revision_id = f"{SOURCE.adapter_version}:{semantic}"
    revision = replace(
        old.revision,
        records=(record,),
        semantic_hash=semantic,
        revision_id=revision_id,
        coverage=ForecastCoverage(1, 1, 1),
        provider_updated_from=record.provider_updated_at,
        provider_updated_to=record.provider_updated_at,
    )
    return ForecastCaptureWrite(
        replace(old.receipt, semantic_hash=semantic, revision_id=revision_id, outcome=outcome),
        old.artifact,
        revision,
    )


def test_unchanged_success_renews_freshness_but_failed_attempt_does_not(tmp_path) -> None:  # type: ignore[no-untyped-def]
    archive = SQLiteForecastArchiveRepository(tmp_path / "archive.sqlite")
    archive.initialize()
    archive.save_capture(capture("old", CUTOFF - timedelta(hours=72)))
    provider = HybridProjectionProvider(history(), archive=archive)
    assert (
        provider.project_batch((TARGET,), scoring_policy=POLICY, decision_time=CUTOFF)[
            0
        ].external_rejection
        == "stale_forecast_receipt"
    )
    archive.save_capture(
        capture("renewed", CUTOFF - timedelta(hours=1), outcome=ForecastCaptureOutcome.UNCHANGED)
    )
    archive.save_capture(failed_capture(receipt_id="failed", persisted_at=CUTOFF))
    result = provider.project_batch((TARGET,), scoring_policy=POLICY, decision_time=CUTOFF)[0]
    assert result.source == "external"
    assert result.forecast.provenance.receipt_id == "renewed"
    assert result.forecast.newer_attempt_receipt_id == "failed"
    assert (
        provider.project(TARGET, scoring_policy=POLICY, decision_time=CUTOFF) == result.projection
    )


def test_removed_player_cannot_reappear_from_an_older_revision(tmp_path) -> None:  # type: ignore[no-untyped-def]
    archive = SQLiteForecastArchiveRepository(tmp_path / "archive.sqlite")
    archive.initialize()
    archive.save_capture(capture("first", CUTOFF - timedelta(hours=1)))
    other = replace(forecast(batch().pools.pool("target").means).forecast, player_id="other")
    archive.save_capture(capture("dropped", CUTOFF, player=other))
    result = HybridProjectionProvider(history(), archive=archive).project_batch(
        (TARGET,), scoring_policy=POLICY, decision_time=CUTOFF
    )[0]
    assert result.external_rejection == "forecast_missing"
    assert result.source == "internal"
    assert result.forecast.provenance.receipt_id == "dropped"


def test_empty_archive_has_internal_output_and_future_receipts_do_not_change_it(tmp_path) -> None:  # type: ignore[no-untyped-def]
    archive = SQLiteForecastArchiveRepository(tmp_path / "archive.sqlite")
    archive.initialize()
    provider = HybridProjectionProvider(history(), archive=archive)
    early = provider.project_batch((TARGET,), scoring_policy=POLICY, decision_time=CUTOFF)
    archive.save_capture(capture("future", CUTOFF + timedelta(seconds=1)))
    assert early == provider.project_batch((TARGET,), scoring_policy=POLICY, decision_time=CUTOFF)


def test_batch_prepares_history_once_and_reads_one_revision_for_multiple_games(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    archive = SQLiteForecastArchiveRepository(tmp_path / "archive.sqlite")
    archive.initialize()
    archive.save_capture(capture("one", CUTOFF))
    calls = []
    original = archive.load_revision_at_cutoff

    def counted(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(archive, "load_revision_at_cutoff", counted)
    provider = HybridProjectionProvider(history(), archive=archive)
    results = provider.project_batch(
        (
            TARGET,
            replace(TARGET, game_id="later", game_start=TARGET.game_start + timedelta(days=1)),
        ),
        scoring_policy=POLICY,
        decision_time=CUTOFF,
    )
    assert len(results) == 2
    assert all(result.source == "external" for result in results)
    assert len(calls) == 1
    with pytest.raises(HybridProjectionError, match="duplicate"):
        provider.project_batch((TARGET, TARGET), scoring_policy=POLICY, decision_time=CUTOFF)


def test_internal_only_does_not_read_archive() -> None:
    class ForbiddenArchive:
        def load_latest_receipt(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("internal-only must not read external evidence")

    provider = HybridProjectionProvider(
        history(), archive=ForbiddenArchive(), config=HybridProjectionConfig(use_external=False)
    )  # type: ignore[arg-type]
    assert provider.project(TARGET, scoring_policy=POLICY, decision_time=CUTOFF) is not None
