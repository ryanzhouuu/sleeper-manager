"""Bind persisted plans to the monotonic audit counts they actually planned against."""

import json

from sleeper_manager.persistence.base import RecommendationRecord
from sleeper_manager.persistence.d1 import D1StateRepository
from sleeper_manager.persistence.rows import recommendation_insert_params
from sleeper_manager.persistence.statements import INSERT_RECOMMENDATION_SQL

_VERSION = """
(SELECT COUNT(*) FROM manager_decision_changes WHERE league_id=? AND fantasy_week=?)
|| ':' ||
(SELECT COUNT(*) FROM acknowledgements a JOIN recommendations r USING(recommendation_id)
 WHERE r.league_id=? AND r.fantasy_week=?)
"""


async def decision_version(repository: D1StateRepository, league_id: str, week: int) -> str | None:
    """Older databases without owner controls retain their previous planning contract."""
    schema = await repository._first(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='manager_decision_changes'"
    )
    if schema is None:
        return None
    row = await repository._first(
        f"SELECT ({_VERSION}) AS version", league_id, week, league_id, week
    )
    return str(row["version"]) if row else None


def plan_matches_version(record: RecommendationRecord, version: str | None) -> bool:
    """Legacy advice is acceptable only when no saved manager actions exist."""
    if version is None:
        return True
    try:
        trace = json.loads(record.trace_json)
        saved = trace.get("manager_decision_version")
        return saved == version or (saved is None and version == "0:0")
    except (ValueError, AttributeError):
        return False


async def create_current_plan(repository: D1StateRepository, record: RecommendationRecord) -> bool:
    """The version comparison and INSERT share one SQL statement, closing the save/publish race."""
    try:
        trace = json.loads(record.trace_json)
        version = trace.get("manager_decision_version")
        if version is None:
            version = trace.get("team_plan", {}).get("manager_decision_version")
    except (ValueError, AttributeError):
        version = None
    if version is None:
        return await D1StateRepository.create_recommendation(repository, record)
    result = await repository._run(
        INSERT_RECOMMENDATION_SQL + f" HAVING ?=({_VERSION})",
        *recommendation_insert_params(record),
        version,
        record.league_id,
        record.fantasy_week,
        record.league_id,
        record.fantasy_week,
    )
    return repository._changes(result) == 1
