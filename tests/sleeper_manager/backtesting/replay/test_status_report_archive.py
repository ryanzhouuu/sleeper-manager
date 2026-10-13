"""Report selection must retain the newest publication and reject changed source bytes."""

import json
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from sleeper_manager.backtesting.replay import status_report_archive as module
from sleeper_manager.integrations.nba.official_injury_models import (
    serialize_official_injury_report_snapshot,
)
from sleeper_manager.integrations.nba.official_injury_parser import (
    official_injury_report_minute_url,
)
from tests.sleeper_manager.workflows.test_game_availability import evidence


def archive_report(path, report, pdf):  # type: ignore[no-untyped-def]
    source = replace(
        report.source,
        provider_id=official_injury_report_minute_url(report.published_at),
        source_updated_at=report.published_at,
        content_hash=sha256(pdf).hexdigest(),
    )
    report = replace(report, source=source)
    path.write_text(
        json.dumps(
            {
                "cache_schema_version": "2",
                "pdf_sha256": sha256(pdf).hexdigest(),
                "snapshot": serialize_official_injury_report_snapshot(report),
            }
        )
    )
    path.with_suffix(".pdf").write_bytes(pdf)
    return report


def test_latest_report_reparsed_without_resurrecting_removed_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    game, _, original = evidence()
    old = archive_report(tmp_path / "old.json", original, b"%PDF-old")
    latest = archive_report(
        tmp_path / "new.json",
        replace(original, published_at=original.published_at + timedelta(minutes=15), entries=()),
        b"%PDF-new",
    )
    archive_report(
        tmp_path / "future.json", replace(original, published_at=game.start_time), b"%PDF-future"
    )
    seen = []
    monkeypatch.setattr(
        module, "extract_official_injury_report_text", lambda pdf: seen.append(pdf) or "reparsed"
    )
    monkeypatch.setattr(
        module,
        "parse_official_injury_report_text",
        lambda text, source: replace(latest, source=source),
    )
    reports, proofs = module.load_status_report_archive(tmp_path, (game,))
    assert reports[game.provider_id] == latest
    assert reports[game.provider_id].entries == ()
    assert seen == [b"%PDF-new"]
    assert {row.content_hash for row in proofs} >= {latest.source.content_hash}
    assert old.source.content_hash not in {row.content_hash for row in proofs}


@pytest.mark.parametrize("corruption", ["pdf", "source_hash", "publication", "url"])
def test_changed_or_mismatched_selected_source_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    game, _, report = evidence()
    path = tmp_path / "report.json"
    archived = archive_report(path, report, b"%PDF-source")
    if corruption == "pdf":
        path.with_suffix(".pdf").write_bytes(b"%PDF-changed")
    elif corruption != "publication":
        payload = json.loads(path.read_text())
        payload["snapshot"]["source"][
            "content_hash" if corruption == "source_hash" else "provider_id"
        ] = "changed"
        path.write_text(json.dumps(payload))
    monkeypatch.setattr(module, "extract_official_injury_report_text", lambda pdf: "text")
    monkeypatch.setattr(
        module,
        "parse_official_injury_report_text",
        lambda text, source: replace(
            archived, published_at=archived.published_at + timedelta(minutes=1)
        ),
    )
    with pytest.raises(ValueError):
        module.load_status_report_archive(tmp_path, (game,))


def test_missing_or_too_old_report_is_a_gap(tmp_path: Path) -> None:
    game, _, report = evidence()
    assert module.load_status_report_archive(tmp_path, (game,)) == ({}, ())
    archive_report(
        tmp_path / "old.json",
        replace(report, published_at=game.start_time - timedelta(days=2)),
        b"%PDF-old",
    )
    assert module.load_status_report_archive(tmp_path, (game,)) == ({}, ())
