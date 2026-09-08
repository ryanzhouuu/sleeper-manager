"""Identity-only recovery requires corroboration and never imports current affiliations."""

import hashlib
import json
from pathlib import Path

import pytest

from sleeper_manager.backtesting.replay.identity_evidence import load_identity_evidence

CATALOG = {"sleeper-1": {"full_name": "Fixture Guard", "birth_date": "1992-03-23"}}


def source_ledger(tmp_path: Path, athletes: list[dict[str, object]]) -> Path:
    raw = json.dumps({"athletes": athletes}).encode()
    (tmp_path / "roster.json").write_bytes(raw)
    path = tmp_path / "identities.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "espn-roster-identities-v1",
                "sources": [
                    {
                        "path": "roster.json",
                        "url": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/6/roster",
                        "retrieved_at": "2026-09-08T00:00:00Z",
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                ],
            }
        )
    )
    return path


def athlete(**changes: object) -> dict[str, object]:
    return {
        "id": "6442",
        "fullName": "Fixture Guard",
        "dateOfBirth": "1992-03-23T08:00Z",
        "team": {"id": "current"},
        "active": True,
        **changes,
    }


def test_identity_recovery_strips_current_team_and_active_state(tmp_path: Path) -> None:
    path = source_ledger(tmp_path, [athlete()])
    evidence = load_identity_evidence(path, CATALOG, ("sleeper-1",))
    assert len(evidence.players) == 1
    player = evidence.players[0]
    assert player.provider_id == "6442"
    assert player.team_id is player.team_abbreviation is player.active is None
    assert player.source.content_hash
    assert len(evidence.source_fingerprints) == 2
    assert load_identity_evidence(path, CATALOG, ()).players == ()


@pytest.mark.parametrize(
    "rows",
    [
        [athlete(dateOfBirth="1992-03-24T08:00Z")],
        [athlete(dateOfBirth=None)],
        [athlete(fullName="Other Person")],
        [athlete(), athlete(id="another")],
        [athlete(), athlete(fullName="Other Person")],
        [athlete(), athlete(dateOfBirth=None)],
    ],
)
def test_uncorroborated_or_conflicting_identity_stays_unresolved(
    tmp_path: Path, rows: list[dict[str, object]]
) -> None:
    assert (
        load_identity_evidence(source_ledger(tmp_path, rows), CATALOG, ("sleeper-1",)).players == ()
    )


def test_corrupt_identity_source_is_rejected(tmp_path: Path) -> None:
    path = source_ledger(tmp_path, [athlete()])
    (tmp_path / "roster.json").write_text('{"athletes": []}')
    with pytest.raises(RuntimeError, match="hash mismatch"):
        load_identity_evidence(path, CATALOG, ("sleeper-1",))


def test_recovery_does_not_resolve_missing_or_bad_catalog_birthdate(tmp_path: Path) -> None:
    path = source_ledger(tmp_path, [athlete()])
    for birth in (None, "invalid"):
        assert (
            load_identity_evidence(
                path,
                {"sleeper-1": {"full_name": "Fixture Guard", "birth_date": birth}},
                ("sleeper-1",),
            ).players
            == ()
        )


def test_identity_recovery_rejects_existing_provider_id_name_conflicts(tmp_path: Path) -> None:
    from dataclasses import replace

    path = source_ledger(tmp_path, [athlete()])
    player = load_identity_evidence(path, CATALOG, ("sleeper-1",)).players[0]
    assert (
        load_identity_evidence(
            path, CATALOG, ("sleeper-1",), (replace(player, full_name="Someone Else"),)
        ).players
        == ()
    )


def test_bundle_uses_recovered_id_without_current_roster_team(tmp_path: Path) -> None:
    from dataclasses import replace
    from datetime import date

    from team_week_projection_support import _nba_inputs
    from team_week_source_support import LEAGUE_ID, RETRIEVED_AT, _write_archive

    from sleeper_manager.backtesting.replay.team_week_bundle import (
        HistoricalTeamWeekBundleError,
        HistoricalTeamWeekBundleRequest,
        bootstrap_historical_team_week_bundle,
    )

    _write_archive(tmp_path)
    catalog_path = tmp_path / "sleeper" / LEAGUE_ID / "players.json"
    catalog = json.loads(catalog_path.read_text())
    catalog["sleeper-1"]["birth_date"] = "1992-03-23"
    catalog_path.write_text(json.dumps(catalog))
    ledger = source_ledger(tmp_path, [athlete(id="provider-1")])
    inputs = replace(_nba_inputs(), provider_players=())
    request = HistoricalTeamWeekBundleRequest(LEAGUE_ID, 4, 16, date(2026, 2, 2))
    with pytest.raises(HistoricalTeamWeekBundleError, match="no joined"):
        bootstrap_historical_team_week_bundle(
            tmp_path, request, nba_inputs=inputs, now=RETRIEVED_AT
        )
    output = bootstrap_historical_team_week_bundle(
        tmp_path, request, nba_inputs=inputs, identity_evidence_path=ledger, now=RETRIEVED_AT
    )
    assert output.team_week.coverage.resolved_identities == 2
    assert output.team_week.coverage.joined_player_games == 2
    assert not output.team_week.coverage.missing_evidence
    assert any(
        source.name == "provider-identity-ledger" for source in output.manifest.source_fingerprints
    )
    assert not inputs.provider_players
