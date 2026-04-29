# Replay Historical POVs on Latest OSS-Fuzz

Use `crsbench replay-povs` when you want to take POVs found in prior CRSBench
bug-finding trials and replay them against the latest OSS-Fuzz project HEAD
and all current harnesses.

This is especially useful for:

- reproducing historical POVs on the current OSS-Fuzz project state
- replaying discovery-only experiment outputs where verification was disabled
- checking whether a crash still reproduces across the full current harness set

Related references:

- [Historical POV replay contract](../../design/evaluation/pov-replay.md)
- [Discovery-only OSS-Fuzz experiments](./discovery-only.md)

## Prerequisites

- One or more completed experiment-output roots containing successful
  bug-finding trials with POVs under `output/povs/`.
- Either a persistent replay cache root via `--cache-root`, or an explicit
  helper/build OSS-Fuzz checkout via `--oss-fuzz-path`.
- A latest-project mirror for `--projects-root`, or permission to let
  CRSBench create and update a managed mirror with `--sync-projects`.
- Local Docker access, because replay builds latest OSS-Fuzz targets and runs
  warm base-runner containers.

Important:

- `--cache-root` is the simplest persistent setup. Replay derives a stable
  helper workspace at `<cache-root>/oss-fuzz-helper` and a managed latest-project
  mirror at `<cache-root>/oss-fuzz-projects`, so later runs can reuse helper
  state and prior `build/out/<project>` results instead of rebuilding from a
  fresh helper checkout.
- `--oss-fuzz-path` is the checkout used for helper-based builds and build
  outputs.
- `--projects-root` is the mirror containing the latest `projects/<name>/`
  tree that replay uses as the source of truth for current project files.
- These paths are intentionally separate so replay can keep a sparse helper
  checkout while still using a full projects mirror.

## Example

Replay POVs from two historical experiment trees, reusing up to eight warm
sessions per `(project, sanitizer)` group and running up to two groups at once:

```bash
uv run crsbench replay-povs \
  --source-dir /data/crsbench/experiment-a \
  --source-dir /data/crsbench/experiment-b \
  --output /tmp/replay-results \
  --cache-root /data/crsbench/replay-cache \
  --sync-projects \
  --jobs 8 \
  --group-jobs 2 \
  --resume \
  --per-pov-timeout 180
```

To use an existing latest-project mirror instead of the managed sync:

```bash
uv run crsbench replay-povs \
  --source-dir /data/crsbench/experiment-a \
  --output /tmp/replay-results \
  --oss-fuzz-path third_party/oss-fuzz \
  --projects-root /srv/oss-fuzz/projects
```

Useful filters:

- repeat `--benchmark <name>` to limit replay to selected benchmarks
- repeat `--trial <id>` to limit replay to selected trial paths
- use a larger `--jobs` value to widen the warm-session pool for one replay group
- use `--group-jobs` to let different `(project, sanitizer)` groups run at the same time; same-project groups still serialize because they share one `build/out/<project>` tree
- use `--resume` to reuse cleanly finished `(project, sanitizer)` groups already checkpointed under the same output directory and rerun only unfinished or changed groups

Discovery-only note:

- if a source experiment uses raw OSS-Fuzz project names such as `gpac` as the
  benchmark identifier, replay accepts those inputs as long as the same
  project exists under `projects_root`

## What Replay Does

For each source POV, replay:

1. discovers replayable POV files from the requested source trees
2. resolves the source benchmark to a latest OSS-Fuzz project
3. builds that latest project for the relevant sanitizer
4. discovers the current runnable fuzz-target wrappers from the build output
5. runs the POV against every current harness through warm reused containers
6. writes artifacts once per physical replay and records provenance for every
   original POV instance

Replay does not try to infer which historical harness produced a POV. The
workflow intentionally replays every POV against every current harness for the
resolved project.

Plain OSS-Fuzz project builds are reused conservatively. Within one
`--oss-fuzz-path` checkout, concurrent replay commands serialize builds per
project, then reuse the result only if the existing `.build-meta.json` matches
the requested sanitizer, the current `projects/<project>` tree fingerprint, and
fuzz target discovery still finds at least one valid harness in
`build/out/<project>/`.

Resource note:

- `--jobs` is per replay group, not global.
- `--group-jobs` multiplies that fanout across different projects. For example,
  `--jobs 40 --group-jobs 2` can keep up to eighty warm replay containers live
  across two projects, plus any active helper build containers.

## Output Layout

Replay writes the following under `--output`:

- `manifest.json`: source roots, helper/projects paths, and runtime settings
- `summary.json`: aggregate counters for mappings, builds, crashes, timeouts,
  errors, plus raw-versus-deduped 0day counters, `crashing_replay_count`,
  explicit original-POV totals, and a `current_run` timing breakdown for the
  emitted crash-only 0day view
- `group-summary.json`: per-`(project, sanitizer)` workload and timing rows,
  including whether a group was reused from `--resume`
- `0day.log`: append-only JSONL stream written as each qualifying
  crash row lands; this is the earliest view during a long scan and is
  deduplicated across `--resume` reruns
- `0day.json`: additive crash-only export that keeps only source entries with at
  least one qualifying replay, and within those entries keeps only those replay
  rows
- `0day-dedup.json`: crash-signature-grouped sibling of `0day.json`, built from
  replay `sanitizer.log` artifacts using the shared crash-signature parser
- `pov-to-crash-map.json`: full mapping from original POV provenance to replay
  artifacts, including non-crashing replay rows
- `.state/groups/<project>/<sanitizer>/group-result.json`: completed replay-group
  checkpoints used by `--resume`
- `artifacts/<project>/<sanitizer>/<target-harness>/<pov-hash>/`: one physical
  replay artifact directory
- `trials/<source-id>/<trial-relative-path>/pov-index.json`: per-trial replay
  index

Each artifact directory contains:

- `stdout.txt`
- `stderr.txt`
- `sanitizer.log`
- `metadata.json`

`pov-to-crash-map.json` includes:

- source experiment identity (`source_id`, `source_dir`, `experiment_name`)
- original trial identity (`trial_relative_path`)
- original POV identity (`original_pov_path`, `original_pov_relpath`,
  `pov_filename`, `pov_content_hash`)
- resolved latest OSS-Fuzz project
- one replay entry per current target harness, with outcome and artifact paths

`0day.json` is derived from that full mapping. Its replay rows are limited to
crashes whose output either contains `AddressSanitizer` or matches the JVM/Jazzer
Java exception patterns already used by POV verification (`== Java Exception:`
or `Exception in thread "`), excluding timeout-like and out-of-memory reports.
Those rows omit `stdout` and `stderr`, while keeping crash-focused fields such
as harness, sanitizer, exit code, duration, artifact directory, sanitizer log,
session restart, and error message.

`0day-dedup.json` keeps the same source-entry provenance but regroups those raw
0day rows by crash signature. Each dedup group records the parsed
`crash_type`, `signature_hash`, `raw_summary`, `signature_source`,
`source_entry_count`, and `replay_count`, then includes only the source entries
and replay rows that match that signature. When a sanitizer log does not parse
cleanly, replay falls back to an exact raw-log hash instead of dropping the row.

`summary.json` now separates the two views explicitly:

- `0day_count` and `0day_raw_count`: raw qualifying source-entry count from
  `0day.json`
- `0day_dedup_count`: crash-signature group count from `0day-dedup.json`
- `crashing_replay_count`: raw qualifying replay-row count retained in
  `0day.json`

It also distinguishes the full replay workload from the work done in the
current invocation:

- `original_pov_instances_total`: total discovered source POV instances used as
  the numerator for `original_pov_instances_per_second`
- `current_run.group_count_total`: all replay groups represented in the output
- `current_run.group_count_executed`: groups actually executed in this
  invocation
- `current_run.group_count_reused`: groups satisfied by `--resume`
- `current_run.physical_replay_tasks_executed`: physical replay tasks executed
  in this invocation
- `current_run.physical_replay_tasks_reused`: physical replay tasks represented
  only by reused checkpoints
- `current_run.timing.*`: current-run wall-clock breakdown, including
  `planning_wall_seconds`, `group_execution_wall_seconds`,
  `finalization_wall_seconds`, plus per-phase active-wall and summed-wall
  totals for lock wait, build/prepare, session-pool setup, replay, and result
  aggregation

`group-summary.json` complements that top-level view. Each row records:

- `mapped_project` and `sanitizer`
- `source_pov_instances`
- `checkpoint_reused`
- `naive_replay_tasks` and `physical_replay_tasks`
- `summary_updates` for that group
- `timing` with per-group phase durations and per-task duration aggregates
- `phase_intervals` with current-run offsets for the measured phases when the
  group executed in this invocation

Rows with `checkpoint_reused: true` keep the checkpointed `summary_updates` and
`timing` totals from the original execution. Filter on
`checkpoint_reused: false` when you need only current-invocation group totals.

`0day.log` is intentionally more granular than `0day.json`: each line is one
source POV plus one qualifying replay row, appended immediately after that
harness finishes. The final `0day.json` later folds those rows back into one
entry per source POV. `0day-dedup.json` is only written during final
aggregation after all replay groups finish.

## Operational Notes

- Replay only scans successful bug-finding trials with visible files in
  `output/povs/`.
- Missing or unsupported mappings are recorded in the output JSON instead of
  aborting the whole run.
- A build failure only blocks the affected `(project, sanitizer)` group.
- `--resume` only reuses groups that finished cleanly and whose discovered input
  signature still matches the current source records. Interrupted or erroring
  groups rerun from scratch on the next invocation.
- `physical_replay_tasks_per_second` and
  `original_pov_instances_per_second` remain output-level effective throughput
  metrics over `elapsed_seconds`; use `current_run` plus `group-summary.json`
  when you need to isolate replay-only wall time or understand `--resume`
  reuse effects.
- Replay checkpoints are format-versioned, so incompatible older group
  checkpoints are ignored instead of being reused under newer replay semantics.
- `--cache-root` is persistent by design. If you need to discard the cached
  helper workspace and force a clean bootstrap, remove
  `<cache-root>/oss-fuzz-helper` before the next run.
- If a rebuild is needed, replay clears the old plain-build metadata before
  invoking `helper.py build_fuzzers`, so a failed rebuild is not treated as a
  reusable cache hit later.
- A replay timeout restarts the warm container once and retries once before
  recording a timeout outcome.
