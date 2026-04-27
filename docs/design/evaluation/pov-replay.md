# Design: Historical POV Replay on Latest OSS-Fuzz
- Audience: maintainers working on evaluation, OSS-Fuzz integration, and replay workflows
- Scope: contract for `crsbench replay-povs`, including input semantics, artifact layout, and failure behavior
- Related: [Evaluation Contract](./evaluation.md), [Distributed Evaluation](../distributed/distributed-evaluation.md), [Replay guide](../../guides/experiments/replay-povs.md)

## Goals and Non-goals

### Goals
- replay POVs from one or more completed experiment-output roots against the latest OSS-Fuzz project state
- run every discovered POV against every currently built fuzz target for the resolved OSS-Fuzz project
- keep replay throughput high by reusing warm base-runner containers instead of creating one fresh container per replay
- persist durable provenance so original POV locations and replay artifacts remain linked after physical execution is deduplicated

### Non-goals
- replacing benchmark-ground-truth verification or CRS scoring
- inferring the original harness that generated a historical POV
- defining a distributed queue-backed replay service in v1

## Constraints

- input roots are completed experiment-output trees, not experiment configs
- replay only consumes successful bug-finding trials with visible files under `output/povs/`
- benchmark-to-project resolution is driven by packaged CRSBench mapping data, with a direct-project fallback for discovery-only experiments whose benchmark name already matches an OSS-Fuzz project directory in `projects_root`
- latest OSS-Fuzz project definitions live in a projects mirror that is distinct from the helper/build checkout used to run `helper.py build_fuzzers`
- replay assumes harnesses are side-effect free across repeated executions in a warm container

## Context and Boundaries

`replay-povs` is an evaluation-adjacent workflow that answers a different question from normal verification:

- normal verification checks a benchmark's authoritative harnesses and verdict semantics against benchmark ground truth
- replay checks whether previously discovered POV blobs still trigger crashes on the latest OSS-Fuzz project HEAD and current harness set

The replay command consumes prior trial outputs, resolves each benchmark to a current OSS-Fuzz project, rebuilds that project for the relevant sanitizer, discovers the current fuzz targets, and fans out all input POVs across all discovered harnesses.

## Contract

### Input Contract

- `--source-dir` is repeatable and each value must point at an existing experiment-output root
- `--output` must be outside every source experiment tree
- `--oss-fuzz-path` identifies the helper/build checkout
- replay requires either an explicit `--projects-root` mirror or `--sync-projects` so latest project definitions are available
- `--benchmark` and `--trial` filters constrain discovery before replay execution begins
- `--resume` reuses completed replay-group checkpoints from the output directory when their input signature still matches the current discovered source POV set

### Project Resolution Contract

- CRSBench first resolves a benchmark name through the packaged benchmark-to-project mapping
- if the mapping explicitly marks a benchmark unsupported, replay records `unsupported_mapping`
- if the mapping is missing but `projects_root/<benchmark>` exists, replay treats that benchmark name as the target OSS-Fuzz project
- if neither mapping nor direct project fallback resolves the benchmark, replay records `missing_mapping`

### Execution Contract

- replay groups work by `(mapped_project, sanitizer)`
- replay may process multiple groups concurrently when configured with `--group-jobs > 1`
- groups for the same mapped project still serialize within one replay process because `build/out/<project>` is shared across sanitizers and warm-session reuse depends on that shared build tree staying stable
- replay is restartable at the `(mapped_project, sanitizer)` group level: a cleanly finished group writes a versioned checkpoint, and `--resume` skips only those completed groups whose source-record signature and checkpoint format both still match
- each group rebuilds the latest project once, then discovers the current runnable fuzz-target wrappers from the build output
- plain latest-project builds are serialized by OSS-Fuzz project within one helper checkout, so concurrent replay commands do not race on the shared `build/out/<project>` directory
- a prior plain build is reusable only when `.build-meta.json` exists, records a non-inc build for the requested sanitizer, and current fuzz target discovery still finds at least one valid harness
- every discovered POV is scheduled against every current target harness for that group
- physical execution is deduplicated by `(mapped_project, sanitizer, target_harness, pov_content_hash)`
- provenance is never deduplicated away: every original POV instance keeps its own replay index entry even when it shares an artifact directory with another source record

### Artifact Contract

For each physically executed replay, CRSBench writes one artifact directory under:

- `artifacts/<mapped-project>/<sanitizer>/<target-harness>/<pov-content-hash>/`

Each artifact directory contains:

- `stdout.txt`
- `stderr.txt`
- `sanitizer.log`
- `metadata.json`

Replay also writes:

- `manifest.json` for command-level inputs and runtime settings
- `summary.json` for aggregate counters, including `0day_count` and `crashing_replay_count`
- `0day.log` for append-only AddressSanitizer JSONL rows emitted as individual qualifying harness results finish
- `0day.json` for an AddressSanitizer-only top-level export
- `pov-to-crash-map.json` for the global mapping from original POV provenance to replay artifacts
- `trials/<source-id>/<trial-relative-path>/pov-index.json` for a per-trial replay index
- `.state/groups/<mapped-project>/<sanitizer>/group-result.json` for completed replay-group checkpoints

`0day.json` is additive and does not replace the full replay index. It contains
only source POV entries with at least one qualifying AddressSanitizer replay,
and each included entry keeps only those replay rows. A qualifying row must be
a replay crash whose output contains `AddressSanitizer`; timeout-like and
out-of-memory reports are excluded from this 0day view. Those rows omit
`stdout` and `stderr` but retain crash-focused fields such as harness,
sanitizer, exit code, duration, artifact directory, sanitizer log, session
restart, and error message.

## Runtime Behavior

### Happy Path

1. discover replayable POV files from all requested source roots
2. resolve each source benchmark to a latest OSS-Fuzz project
3. repoint `oss_fuzz_path/projects/<project>` at the latest project mirror
4. build the target project once per `(mapped_project, sanitizer)`
5. discover current fuzz targets from the fresh build output
6. execute deduplicated replay tasks through a warm session pool
7. classify outcomes using the same crash/timeout/leak semantics as helper-based reproduce
8. write artifacts once per physical replay, emit both the full replay mapping and
   AddressSanitizer-only `0day.json`, and re-index them for every original source POV

If another replay process is already building the same OSS-Fuzz project in the
same checkout, later processes wait on that per-project lock and re-check the
result after the lock is released instead of rebuilding blindly.

`summary.json` reports `0day_count` and `crashing_replay_count` for the emitted
AddressSanitizer-only view: source entries included in `0day.json` and replay
rows kept there, not deduplicated physical replay tasks.

`0day.log` is deliberately earlier and more granular than `0day.json`. Each line
contains one source record plus one qualifying AddressSanitizer replay row and
is flushed to disk immediately so operators can tail live results during long
scans. The final `0day.json` remains the aggregated AddressSanitizer-only
export.

### Warm Session Behavior

- each warm session starts one long-lived base-runner container for a specific project build
- replay runs execute through `docker exec` inside that container
- `--jobs` limits warm sessions per replay group; `--group-jobs` limits how many distinct groups can be active at once
- a replay timeout forces one container restart and one retry for that session
- if the retry also times out, replay records a `timeout` outcome for that physical task

### Failure Behavior

- unresolved mappings produce `missing_mapping` or `unsupported_mapping` without aborting unrelated work
- a missing latest project directory produces `target_project_missing`
- build failures produce `build_error` for every record in that `(mapped_project, sanitizer)` group
- interrupted or erroring groups do not write a resume checkpoint, so `--resume` reruns them completely on the next invocation while preserving any previously appended `0day.log` crash lines
- when a rebuild is required, stale plain-build metadata is discarded before the build starts so a failed rebuild cannot leave a false reusable cache marker behind
- session start or replay runtime failures produce `error`
- one group's failure must not abort replay for unrelated groups

## Deployment/Distributed Behavior

- v1 replay is single-host and assumes local Docker access
- the task model, summary outputs, and artifact index files are intentionally serialization-friendly so replay can move to queue-backed or remote execution later without changing user-visible artifact contracts
- the helper checkout and latest projects mirror are separate inputs so remote execution can mount or synchronize them independently

## Decisions and Tradeoffs

- decision: reuse warm base-runner containers instead of calling `helper.py reproduce` for every replay
  - tradeoff: more container lifecycle code, substantially lower per-replay overhead
- decision: replay every POV against every current harness
  - tradeoff: more executions, but no fragile coupling to historical harness naming
- decision: preserve per-source provenance even after deduplicating physical execution
  - tradeoff: more indexing metadata, but artifact reuse stays observable and auditable

## Risks and Validation

- packaged mappings can lag OSS-Fuzz project renames; direct-project fallback reduces but does not eliminate this risk
- latest projects can fail to build even when the historical trial succeeded
- base-runner compatibility depends on selecting an image compatible with the project's `base_os_version`
- validation should cover multi-root discovery, project sync behavior, warm-session reuse, deduplicated artifact writing, and CLI path validation

## Implementation Pointers

- `crsbench/evaluation/replay/discovery.py`
- `crsbench/evaluation/replay/projects.py`
- `crsbench/evaluation/replay/session.py`
- `crsbench/evaluation/replay/engine.py`
- `crsbench/evaluation/replay/cli.py`
