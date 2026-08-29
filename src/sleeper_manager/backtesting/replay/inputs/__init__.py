"""Public compatibility surface for historical replay-input assembly."""

from sleeper_manager.backtesting.replay.inputs.artifact import (
    HistoricalTeamWeekArtifactError,
    load_historical_team_week_artifact,
)
from sleeper_manager.backtesting.replay.inputs.assembly import (
    assemble_historical_team_week_inputs,
)
from sleeper_manager.backtesting.replay.inputs.manifest import (
    build_replay_input_manifest,
    records_fingerprint,
    source_fingerprint,
    write_replay_input_bundle,
)
from sleeper_manager.backtesting.replay.inputs.models import (
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
    "HistoricalTeamWeekArtifactError",
    "HistoricalTeamWeekInput",
    "ReplayCoverageSummary",
    "ReplayInputError",
    "ReplayInputExclusion",
    "ReplayInputManifest",
    "SourceFingerprint",
    "assemble_historical_team_week_inputs",
    "build_replay_input_manifest",
    "load_historical_team_week_artifact",
    "records_fingerprint",
    "source_fingerprint",
    "write_replay_input_bundle",
)
