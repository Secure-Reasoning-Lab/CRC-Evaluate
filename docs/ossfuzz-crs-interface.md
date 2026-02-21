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
- `--target-proj-path <trial>/staged/<benchmark>`
- `--target-harness <harness-name>` (run phase)
- `--sanitizer <address|memory|undefined>`
- `--run-id <id>` (artifacts + run)

### Why `oss-crs artifacts` matters

CRSBench no longer relies on glob-based submit-dir discovery. Instead, it resolves paths from `oss-crs artifacts` and uses those exact paths for collection and runtime monitoring.

## Input/Output Contract

### Benchmark Input

CRSBench stages benchmark files (excluding dot-directories such as `.aixcc/`) before `build-target`/`run`.

### Runtime Inputs (when applicable)

- `--seed-dir <dir>` for seed corpus
- `--diff <file>` for bug-fixing delta context
- `--pov <file>` or `--pov-dir <dir>` for bug-fixing
- harness source/hints resolved from benchmark metadata where applicable

### Expected CRS Outputs

CRS outputs are read from resolved `SUBMIT_DIR`/`EXCHANGE_DIR` paths from `oss-crs artifacts`.

Typical collected outputs:

- Bug-finding:
  - `SUBMIT_DIR/povs/`
  - `SUBMIT_DIR/seeds/`
- Bug-fixing:
  - `SUBMIT_DIR/patches/`
  - optional `SUBMIT_DIR/povs/`

CRSBench copies these into trial output directories for verification/reporting.

## Verification and Dedup

Discovered POVs are deduplicated before verification based on `pov_dedup_strategy` in experiment config:

- `patch-based` (default)
- `stack-based`
- `status-based`
- `none`

## Upstream OSS-CRS References

- `oss-crs/README.md` — quick start and lifecycle overview
- `oss-crs/docs/design/parallel.md` — `--run-id`, `artifacts`, and deterministic path semantics
- `oss-crs/docs/config/crs-compose.md` — compose config reference
- `oss-crs/docs/config/crs.md` — CRS config (`oss-crs/crs.yaml`) reference
- `oss-crs/docs/design/libCRS.md` — submit/fetch artifact channels and shared dirs

## Related CRSBench Docs

- [Repository README](../README.md)
- [Experiment Workflow](./experiment-workflow.md)
- [Experiment Config Example](./experiment-config-example.yaml)
- [Design: oss-crs Integration](./design/evaluation/oss-crs-integration.md)
