# OSS-CRS Interface (Current)

This document describes the current CRS execution contract used by CRSBench.

## Overview

CRSBench uses the unified `oss-crs` CLI for both bug-finding and bug-fixing CRS workflows through `OssCrsAdapter`.

Execution lifecycle per trial:

1. `oss-crs prepare`
2. `oss-crs build-target`
3. `oss-crs artifacts`
4. `oss-crs run`

CRSBench generates trial-local compose/work directories, resolves deterministic artifact paths using `--run-id`, then collects outputs from resolved paths.

## Coverage Lifecycle

`crsbench coverage` intentionally uses a narrower `oss-crs` contract than trial
execution:

1. `crsbench prepare --coverage` runs Atlantis `oss-crs prepare` against the
   repository-local Team Atlanta `third_party/atlantis-multilang-given_fuzzer`
   checkout only when the canonical GHCR images are not already available.
2. The first coverage request for a benchmark runs Atlantis
   `oss-crs build-target` and normalizes the build output into the layout the
   warm coverage runtime expects.
3. CRSBench then starts one warm coverage container per
   `(benchmark, harness, shard)` and drives libCRS/UniAFL directly for per-seed
   coverage collection.

Coverage mode does not use `oss-crs artifacts` or `oss-crs run`. Those phases
remain part of the trial execution contract for bug-finding and bug-fixing.

## Command Model

CRSBench invokes `oss-crs` with these core arguments:

- `--compose-file <trial>/crs-compose.yaml`
- `--work-dir <trial>/oss-crs-workdir`
- `--target-path <trial>/staged/<benchmark>` (`--target-proj-path` compatibility alias)
- `--target-harness <harness-name>` (run phase)
- `--sanitizer <address|memory|undefined>`
- `--run-id <id>` (artifacts + run)

Note: CRSBench currently does not pass `--target-source-path`; staged benchmark
input under `--target-path` is the single source input path in this integration.

### Why `oss-crs artifacts` matters

CRSBench no longer relies on glob-based submit-dir discovery. Instead, it resolves paths from `oss-crs artifacts` and uses those exact paths for collection and runtime monitoring.

## Input/Output Contract

### Benchmark Input

CRSBench stages benchmark files (excluding dot-directories such as `.aixcc/`) before `build-target`/`run`.

### Runtime Inputs (when applicable)

- `--seed-dir <dir>` for seed corpus
- `--diff <file>` for bug-fixing delta context
- `--pov <file>` or `--pov-dir <dir>` for bug-fixing
- SARIF bug-candidate hints selected through `runtime.inputs.sarif.level`
- harness source resolved from benchmark metadata where applicable

### Coverage Runtime Inputs

For `crsbench coverage`, CRSBench supplies:

- the normalized Atlantis build output for the selected benchmark
- the harness name from trial metadata or direct CLI input
- a mounted seed directory (`trial/output/seeds/`, legacy `trial/output/corpus/`,
  or direct `--seed-dir`)
- one pinned CPU per warm coverage container

Timeline origin depends on the entry point:

- direct `--seed-dir` mode uses the first retained seed `mtime` as time origin
  unless `--experiment-start-time <unix-seconds>` is provided
- `--experiment-dir` and `--experiment-config` use
  `povs/pov_store.json.crs_run_start_time`, preserve all normalized seeds by
  clamping pre-start inputs to `0.0`, and bound the x-axis to
  `metadata.json.run_time`

`--jobs` controls how many `(benchmark, harness)` coverage jobs run in parallel.
`--cores-per-job` controls how many one-core warm containers are used for a
single job; CRSBench splits the seed set into that many shards and runs each
shard sequentially inside its own warm runner.

### LiteLLM Runtime Contract

CRSBench currently supports only the external LiteLLM model when a run needs
LLM access.

- supported today: `runtime.litellm.mode: external`
- supported today: `runtime.litellm.skip: true`
- not implemented yet: `runtime.litellm.mode: self_hosted`

CRSBench resolves the external LiteLLM endpoint and credentials from the
canonical `CRSBENCH_LLM_*` environment contract, then passes the resolved
runtime settings through the generated `oss-crs` execution environment.

### Expected CRS Outputs

CRS outputs are read from resolved `SUBMIT_DIR`/`EXCHANGE_DIR` paths from `oss-crs artifacts`.

Typical collected outputs:

- Bug-finding:
  - `SUBMIT_DIR/povs/`
  - `SUBMIT_DIR/seeds/`
- Bug-fixing:
  - `SUBMIT_DIR/patches/`
  - optional `SUBMIT_DIR/povs/`
- Logs:
  - top-level run logs from `run_logs`
  - per-CRS run logs from `crs.<name>.run_logs`
  - per-CRS internal/agent logs from `crs.<name>.log_dir`

CRSBench copies these into trial output directories for verification/reporting.

Coverage runs additionally persist per-seed raw artifacts under
`trial/coverage/raw/`, including:

- `<seed-hash>.cov`
- `<seed-hash>.stdout.log`
- `<seed-hash>.stderr.log`
- optional `<seed-hash>.crash.log`
- worker logs such as `worker.stdout.log` or `worker.worker-<n>.stdout.log`

## Coverage Workflow

`crsbench coverage` uses a thinner `oss-crs` contract than full CRS runs:

1. `crsbench prepare --coverage` writes a single-CRS compose file that points at
   the repository-local Atlantis checkout and runs `oss-crs prepare` only as a
   fallback after GHCR image resolution.
2. Coverage collection lazily stages each benchmark and runs
   `oss-crs build-target`.
3. CRSBench resolves the Atlantis build output from the `oss-crs` workdir,
   normalizes it into the legacy runtime layout, then starts warm coverage
   containers directly for per-seed execution.

Runtime semantics for coverage:

- One warm container is pinned to one CPU.
- `--jobs` controls how many `(benchmark, harness)` jobs run in parallel.
- `--cores-per-job` controls how many warm containers are started for the same
  `(benchmark, harness)` job; CRSBench shards the seed corpus across them.
- Seeds remain sequential inside each warm container so libCRS/UniAFL state is
  reused safely.

Post-trial coverage outputs are written under `trial/coverage/` and include:

- `coverage_timeline.json` with one entry per normalized seed plus the final summary
- `coverage_timeline.csv` with one row per normalized seed
- `coverage_timeline.png`
- raw per-seed artifacts under `coverage/raw/`

### Log Preservation Contract

CRSBench preserves resolved `oss-crs artifacts` log paths under:

- `trial/output/logs/docker-compose.stdout.log`
- `trial/output/logs/docker-compose.stderr.log`
- `trial/output/logs/services/`
- `trial/output/logs/crs/<crs>/...` for per-CRS run logs
- `trial/output/logs/crs/<crs>/log_dir/...` for per-CRS internal/agent logs from `LOG_DIR`

This copied trial output is the durable result location. `cleanup_after_trial`
removes internal workdirs such as `oss-crs-workdir`, but does not remove
`trial/output/`, so copied logs remain available after cleanup. `cloud collect`
applies the same durable-artifact contract: the main artifact rsync excludes
`oss-crs-workdir/`, and any top-level trial entries that still symlink into
that workdir are materialized into the collected tree before publish.

## Verification and Dedup

In `VerificationEngine.verify_benchmark`-based flows, `pov_dedup_strategy` controls post-verification deduplication of POV results:

- `patch-based` (default)
- `stack-based`
- `status-based`
- `none`

Live bug-finding experiment runs use `POVVerificationManager`, which applies hash-based pre-verification filtering/skip logic rather than a post-verification strategy pass.

Additional notes for bug-finding re-eval:
- When re-eval verifies POV files from a trial directory, it first applies a per-content-hash cap (`max_per_hash=1`) so only one file per identical payload is verified.
- File selection is deterministic by filename order.
- Both synchronous and asynchronous/distributed re-eval persist raw verification results after this hash-capped selection step (no additional post-verification dedup strategy pass).

## Upstream OSS-CRS References

- [oss-crs/README.md](../../oss-crs/README.md) — quick start and lifecycle overview
- [oss-crs/docs/design/parallel.md](../../oss-crs/docs/design/parallel.md) — `--run-id`, `artifacts`, and deterministic path semantics
- [oss-crs/docs/config/crs-compose.md](../../oss-crs/docs/config/crs-compose.md) — compose config reference
- [oss-crs/docs/config/crs.md](../../oss-crs/docs/config/crs.md) — CRS config (`oss-crs/crs.yaml`) reference
- [oss-crs/docs/design/libCRS.md](../../oss-crs/docs/design/libCRS.md) — submit/fetch artifact channels and shared dirs

## Related CRSBench Docs

- [Repository README](../../README.md)
- [Distributed Experiments](../guides/experiments/distributed.md)
- [Experiment Config Example](../experiment-config-distributed-example.yaml)
- [Design: oss-crs Integration](../design/evaluation/oss-crs-integration.md)
