"""Public compatibility surface for historical replay-input assembly."""

from sleeper_manager.backtesting.historical_replay_assembly import (
    assemble_historical_team_week_inputs,
)
from sleeper_manager.backtesting.replay_input_manifest import (
    build_replay_input_manifest,
    records_fingerprint,
    source_fingerprint,
    write_replay_input_bundle,
)
from sleeper_manager.backtesting.replay_input_models import (
    HistoricalReplayBuildInput,
    HistoricalTeamWeekInput,
    ReplayCoverageSummary,
    ReplayInputError,
    ReplayInputExclusion,
    ReplayInputManifest,
    SourceFingerprint,
)

__all__ = (
    "HistoricalReplayBuildInput",
    "HistoricalTeamWeekInput",
    "ReplayCoverageSummary",
    "ReplayInputError",
    "ReplayInputExclusion",
    "ReplayInputManifest",
    "SourceFingerprint",
    "assemble_historical_team_week_inputs",
    "build_replay_input_manifest",
    "records_fingerprint",
    "source_fingerprint",
    "write_replay_input_bundle",
)
