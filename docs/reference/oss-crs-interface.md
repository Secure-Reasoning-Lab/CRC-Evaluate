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

### Log Preservation Contract

CRSBench preserves resolved `oss-crs artifacts` log paths under:

- `trial/output/logs/docker-compose.stdout.log`
- `trial/output/logs/docker-compose.stderr.log`
- `trial/output/logs/services/`
- `trial/output/logs/crs/<crs>/...` for per-CRS run logs
- `trial/output/logs/crs/<crs>/log_dir/...` for per-CRS internal/agent logs from `LOG_DIR`

This copied trial output is the durable result location. `cleanup_after_trial`
removes internal workdirs such as `oss-crs-workdir`, but does not remove
`trial/output/`, so copied logs remain available after cleanup.

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
