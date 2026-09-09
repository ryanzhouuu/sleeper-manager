# Historical input inventories

The file-based input index selects immutable replay bundles and accounts for a
complete declared sample. It does not acquire sources, execute an advisor, infer a
latest bundle, or evaluate release gates. In particular, an assembled observed-
starter diagnostic is not proof of full-advisor replay readiness.

Run an inventory from a selected index specification:

```sh
python -m sleeper_manager.backtesting.experiments.input_index selected-index.json \
  --output-root /path/to/inventories
```

The command validates all referenced file hashes before writing
`<output-root>/<index-id>/index.json` and `inventory.json`. References can be
absolute or relative to the authoring index. Persisted indexes use absolute paths,
so copying the index does not change reference resolution. Moving the source files
requires a new index. Identical reruns preserve bytes; conflicting output cannot
be overwritten.

## Index contract

`ExperimentInputIndex` and its nested models live in
`backtesting.experiments.input_index_models`. Unknown fields, invalid hashes,
nonpositive week/roster IDs and duplicate sample dimensions are rejected.

| Field | Meaning |
| --- | --- |
| `schema_version` | `experiment-input-index-v1` |
| `protocol` | Retained protocol file reference (`path`, `sha256`) |
| `executor_commit` | Full 40-character Git revision used for this inventory |
| `variant` | Explicit configuration/variant label; not a qualification claim |
| `builder_version`, `projection_config_version`, `eligibility_policy_version` | Required manifest compatibility bindings |
| `policy_configuration` | Optional retained configuration reference; null when unbound |
| `seeds` | Explicit nonnegative seeds; empty when no simulation is run |
| `unresolved_bindings` | Remaining protocol/executor/configuration decisions |
| `leagues` | Declared league-season samples, each with role, roster IDs, weeks, configuration/scoring fingerprints and supporting file references |
| `selections` | Explicit bundle or pre-assembly failure for a logical team-week; omitted keys remain unprocessed |

Each league's `weeks` entry has `week` and nullable `boundaries_fingerprint`.
An unbound week can remain in the inventory but cannot accept a bundle. A league
can also declare `shared_sources` (`name`, `content_hash`, `version`); each selected
manifest must contain those bindings. This allows shared raw datasets to be fixed
while per-roster evidence and timelines differ. These bindings and the protocol
reference make compatibility constraints reviewable; they do not certify that
an unfinished experiment has been fully frozen.

Each selection contains a `key` (`league_id`, `season`, `week`, `roster_id`) and
exactly one of:

- `bundle`: file references named `manifest` and `team_week`, including for
  assembled-but-unusable bundles. Coverage and exclusions come from that artifact.
- `failure`: a stable `reason`, explanatory `detail`, and retained `evidence`
  reference. `stage` defaults to `before_assembly`; use `artifact_validation`
  when a produced artifact fails validation, retaining its references in the
  failure evidence. This records the failure without admitting the invalid bundle.

Optional `outputs` retain hashed result references without treating them as a
validated replay result. The helper `file_reference(Path(...))` captures absolute
paths and hashes. `build_input_inventory(index, base=...)` validates a typed index;
`write_input_inventory(index_path, output_root)` is the persisted API.

Identical selections count once. Conflicting selections for the same logical key
raise an error, including a failed attempt competing with another version. The
caller must explicitly choose a version in a new index; previous immutable
indexes retain earlier attempts. Out-of-sample keys are rejected. Manifest content
identities are recomputed, then league, season, scoring, configuration, calendar,
version, shared-source and nested projection bindings are checked. Different
leagues use their own manifests; no cross-league manifest is required.

## Accounting and limits

Every expected key has one status: `unprocessed`, `failed_before_assembly`, `failed_artifact_validation`,
`incomplete`, `empty`, or `assembled`. Missing work and evidence failures stay in
the denominator. `assembled` means the selected artifact has matching opportunity,
identity, eligibility, outcome and projection counts, retained player-games, and
no reported exclusions or missing evidence. It does not check full chronological
lineup execution or actual decision-cutoff availability.

The inventory retains original coverage counts, exclusions, eligibility quality,
strict completeness and source references. Approximate inputs can be assembled
while `strict_complete` remains false. Empty artifacts never become replay-ready
merely because zero counts match. League summaries keep denominators, status
counts, strict-complete counts and unbound calendar weeks separate.

`replay_readiness` is always `not_evaluated`: this command measures input assembly,
not the historical readiness release gate. It does not select structural sample
exclusions or remove failures from the denominator. The older generic Lock-In
validation command retains its legacy directory-scan summary; use this explicit
index for multi-league sample accounting.
