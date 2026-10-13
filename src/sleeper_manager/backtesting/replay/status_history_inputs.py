"""Load schedule and stable identities without materializing historical play-by-play."""

import json
from datetime import UTC, datetime
from pathlib import Path

from sleeper_manager.backtesting.experiments.data import (
    HistoricalExperimentInputs,
    _collect_identities,
    _default_rds_reader,
    _load_resource,
    _regular_schedule_rows,
    _regular_season_rows,
)
from sleeper_manager.backtesting.replay.identity_evidence import load_identity_evidence
from sleeper_manager.backtesting.replay.inputs import SourceFingerprint
from sleeper_manager.domain.nba import ProviderPlayer, ScheduledGame, Team
from sleeper_manager.integrations.nba.sportsdataverse import (
    parse_schedule_rows,
    player_box_score_url,
    schedule_url,
)

_IDENTITY_COLUMNS = (
    "season_type",
    "athlete_id",
    "athlete_display_name",
    "team_id",
    "team_abbreviation",
    "team_display_name",
    "team_location",
    "active",
)


def load_status_history_inputs(
    raw_dir: Path,
    seasons: tuple[int, ...],
    *,
    identity_ledger: Path | None = None,
    sleeper_catalog: Path | None = None,
) -> tuple[HistoricalExperimentInputs, tuple[SourceFingerprint, ...]]:
    """Recover identity only; reviewed per-game censuses supply historical affiliations."""
    if (identity_ledger is None) != (sleeper_catalog is None):
        raise ValueError("Identity recovery needs both a ledger and catalog")
    now = datetime.now(UTC)
    games: list[ScheduledGame] = []
    artifacts = []
    teams: dict[str, Team] = {}
    players: dict[tuple[str, str], ProviderPlayer] = {}
    for season in seasons:
        for resource, filename, url, columns in (
            ("schedule", "nba_schedule", schedule_url(season), None),
            ("player_identities", "player_box", player_box_score_url(season), _IDENTITY_COLUMNS),
        ):
            rows, artifact = _load_resource(
                raw_dir / str(season) / f"{filename}_{season}.rds",
                season=season,
                resource=resource,
                url=url,
                reader=_default_rds_reader,
                columns=columns,
            )
            artifacts.append(artifact)
            if resource == "schedule":
                games.extend(
                    parse_schedule_rows(_regular_schedule_rows(rows), retrieved_at=now).records
                )
            else:
                _collect_identities(
                    (
                        row
                        for row in _regular_season_rows(rows)
                        if str(row.get("athlete_id", "")).strip() not in ("", "nan")
                    ),
                    retrieved_at=now,
                    teams=teams,
                    players=players,
                )
    proofs = [
        SourceFingerprint(f"{row.resource}:{row.season}", row.sha256, "rds-v1") for row in artifacts
    ]
    identities = tuple(players.values())
    if identity_ledger is not None and sleeper_catalog is not None:
        catalog = json.loads(sleeper_catalog.read_bytes())
        if not isinstance(catalog, dict) or not all(
            isinstance(value, dict) for value in catalog.values()
        ):
            raise ValueError("Invalid identity catalog")
        evidence = load_identity_evidence(identity_ledger, catalog, tuple(catalog), identities)
        identities += evidence.players
        proofs.extend(evidence.source_fingerprints)
    return HistoricalExperimentInputs(
        tuple(games), (), (), tuple(teams.values()), identities, tuple(artifacts), 0
    ), tuple(proofs)
