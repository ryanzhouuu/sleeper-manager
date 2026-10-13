"""Select latest pre-cutoff reports, then verify and reparse their retained PDF bytes."""

import json
from bisect import bisect_right
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path

from sleeper_manager.backtesting.replay.inputs import SourceFingerprint
from sleeper_manager.domain.nba import ScheduledGame
from sleeper_manager.integrations.nba.official_injury_models import (
    PARSED_REPORT_CACHE_SCHEMA_VERSION,
    OfficialInjuryReportSnapshot,
    deserialize_official_injury_report_snapshot,
)
from sleeper_manager.integrations.nba.official_injury_parser import (
    extract_official_injury_report_text,
    official_injury_report_urls,
    parse_official_injury_report_text,
)


def load_status_report_archive(
    archive: Path,
    games: tuple[ScheduledGame, ...],
    *,
    cutoff_minutes: int = 30,
) -> tuple[dict[str, OfficialInjuryReportSnapshot], tuple[SourceFingerprint, ...]]:
    """A newer removed row never resurrects an older status; invalid selected evidence fails."""
    indexed: dict[datetime, Path] = {}
    for path in sorted(archive.glob("*.json")):
        payload = json.loads(path.read_bytes())
        published = datetime.fromisoformat(payload["snapshot"]["published_at"])
        if published.utcoffset() is None or published in indexed:
            raise ValueError("Ambiguous archived report publication")
        indexed[published] = path
    publications = sorted(indexed)
    selected = {}
    for game in games:
        cutoff = game.start_time - timedelta(minutes=cutoff_minutes)
        index = bisect_right(publications, cutoff) - 1
        if index >= 0 and cutoff - publications[index] <= timedelta(hours=24):
            selected[game.provider_id] = indexed[publications[index]]
    verified = {}
    proofs: list[SourceFingerprint] = []
    for path in sorted(set(selected.values())):
        encoded = path.read_bytes()
        payload = json.loads(encoded)
        if payload["cache_schema_version"] != PARSED_REPORT_CACHE_SCHEMA_VERSION:
            raise ValueError("Unsupported archived report schema")
        cached = deserialize_official_injury_report_snapshot(payload["snapshot"])
        pdf = path.with_suffix(".pdf").read_bytes()
        digest = sha256(pdf).hexdigest()
        if (
            not pdf.startswith(b"%PDF-")
            or digest != payload["pdf_sha256"]
            or digest != cached.source.content_hash
            or cached.source.source_updated_at != cached.published_at
            or cached.source.provider_id not in official_injury_report_urls(cached.published_at)
        ):
            raise ValueError("Archived report source verification failed")
        parsed = parse_official_injury_report_text(
            extract_official_injury_report_text(pdf), source=cached.source
        )
        if parsed.published_at != cached.published_at:
            raise ValueError("Archived PDF publication mismatch")
        verified[path] = parsed
        proofs.extend(
            (
                SourceFingerprint(f"injury-pdf:{digest}", digest, "pdf-v1"),
                SourceFingerprint(
                    f"injury-metadata:{path.name}", sha256(encoded).hexdigest(), "json-v2"
                ),
            )
        )
    return {game: verified[path] for game, path in selected.items()}, tuple(proofs)
