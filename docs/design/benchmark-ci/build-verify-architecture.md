# Build-Verify Architecture

## Overview

This document defines how builds and verification work across CRSBench commands. Core principle for benchmark-ci DAG commands (`build`, `pov`, `patch`, `coverage`, `all`): **snapshot mode by default (`--mode snapshot`), full mode available via `--mode full`, cache reuse by default**.

## Build Modes

### Snapshot Mode (Default for `benchmark ci`)

Uses incremental build flow and shared vulnerable snapshots for CI checks.

### Full Build

Builds using base OSS-Fuzz images without incremental-image flow.

### Snapshot Build (Default)

Uses pre-built Docker images (`ghcr.io/team-atlanta/crsbench/{project}-{san}:inc`) with pre-compiled dependencies. Only recompiles changed files. CI `--mode snapshot` enables this flow by default.

Falls back to full build when:
- Inc-build image not available
- Inc-build fails (automatic fallback → PASS-FB)

### Cache

Builds are cached in `oss-fuzz/build/out/{variant_name}/`. By default, if a cached build exists and matches the requested mode, it's reused.

## Command Defaults

| Command | Build Mode | Cache | Force Rebuild | Fallback |
|---------|-----------|-------|---------------|----------|
| `crsbench verify` | full | ON | `--force-rebuild` | N/A |
| `crsbench patch-verify` | full | ON | `--force-rebuild` | N/A |
| `crsbench coverage` | full | ON | `--force-rebuild` | N/A |
| `crsbench run` | full | ON | `--force-rebuild` | N/A |
| `crsbench benchmark ci {build,pov,patch,coverage,all}` | snapshot | ON | `--force-rebuild` | N/A |
| `crsbench benchmark ci retry` | full (default) | ON | `--force-rebuild` | N/A |

All commands support explicit `--mode` where applicable (`snapshot` or `full`).

### Standalone Commands

```
--mode {snapshot,full}   Select snapshot-build or full-build flow
--force-rebuild          Ignore cache, rebuild from scratch
```

Default behavior for standalone commands (full build):
```
Has cached build? ──yes──→ Use cached build
       │no
       ▼
Full build
```

With `--mode snapshot`:
```
Has cached build? ──yes──→ Use cached build
       │no
       ▼
Inc-image available? ──yes──→ Build with inc-image
       │no                          │
       ▼                       Build failed?
Full build (fallback)               │yes
                                    ▼
                              Fallback to full build (PASS-FB)
```

### CI Commands

CI defaults to snapshot mode with cache reuse. To force full mode, use `--mode full`. To compare both modes, run CI twice:
```bash
# Test with snapshot mode (default)
crsbench benchmark ci all --all --output-dir results-snapshot

# Test with full mode
crsbench benchmark ci all --all --mode full --output-dir results-full

# Compare results externally
diff results-snapshot/summary.json results-full/summary.json
```

## CI DAG Architecture

### Single-Mode Execution

Each CI invocation runs **one build mode** (snapshot by default). All checks fan out in parallel after the initial build.

```
START ── build ──┬── verify-pov(pov1)
                 ├── verify-pov(pov2)
                 ├── build-patch(p1) ── test(p1, full)
                 ├── build-patch(p2) ── test(p2, full)
                 └── collect-coverage
```

### Why Single-Mode (Not Two-Phase)

Both full and inc builds write to the same filesystem path:
```
oss-fuzz/build/out/{variant_name}/
```

`helper.py reproduce` reads fuzzer binaries from this path at runtime. Running both modes in one invocation would require:
- Sequential phases (full completes, then inc starts)
- Waiting for all verify jobs before next build

Single-mode is simpler: one build, parallel fan-out, done. Compare modes externally by running twice.

### Parallelism

After the initial build, all jobs fan out in parallel:
- All `verify-pov` jobs run concurrently (read-only against Docker image)
- All `build-patch` jobs run concurrently (each creates its own variant name)
- `collect-coverage` runs concurrently with verify/patch jobs

### Distributed Build-Context Hydration

In distributed CI, verify workers must not rebuild variants that were already
built by build workers. Build context loading therefore follows this rule:

- `allow_build=False` on verify paths
- on cache miss in memory, hydrate from on-disk build cache metadata
- hard-fail only when required variants are genuinely missing from disk

This preserves build/verify separation while avoiding cold-worker false errors
from empty in-memory caches.

### Patch Queue Identity and Dedup

Patch build/verify jobs use deterministic queue `job_id` values derived from:

- experiment/trial identifiers
- benchmark + harness + CPV + patch ID
- execution mode fields (`source_mode`, `test_mode`, `verify_variants`,
  `use_inc_build`, sanitizer as applicable)

If enqueue sees a duplicate, CRSBench reuses the existing job instead of
creating a second concurrent copy. Patch verify jobs also consume the real
upstream patch-build RQ job ID from payload metadata (no local reconstruction).

### Inc-Image Local Build Pull Policy

Local inc-image preparation uses `helper.py build_image --no-pull` by default
to avoid forced registry refreshes in hot CI paths. Explicit image refreshes
should be done via dedicated preflight/pull steps.

### Removed CI Subcommands and Flags

- **`ci inc-build`**: Removed
- **`--check-inc` on `ci pov`/`ci patch`**: Removed — use `--mode full` or `--mode snapshot`

## Timing Breakdown

Each check type reports timing differently based on what's being measured:

| Check | Timing | Rationale |
|-------|--------|-----------|
| POV | `B:Xs V:Ys` | Build and verify are separate phases |
| Patch | Total (e.g., `30s`) | Rebuild is part of verification (apply patch → rebuild → test) |
| Coverage | `B:Xs V:Ys` | Build and collection are separate phases |

### Per-Patch Detail

Patch checks include per-patch breakdown in `details`:
```json
{
  "per_patch": [
    {"patch_id": "patch_0", "build_time": 5.2, "unit_test_time": 12.3},
    {"patch_id": "patch_1", "build_time": 4.8, "unit_test_time": 11.1}
  ]
}
```

## Current Architecture (Nested ThreadPoolExecutors)

### POV Engine (`evaluation/verification/pov/engine.py`)

```
verify_benchmark()
  → _get_or_build_results()          # Sequential build
  → verify_povs_parallel()           # ThreadPoolExecutor
      → [(pov, harness, variant)]    # Flattened task list
      → reproduce() per task
```

`verify_benchmark()` hash-skip semantics:
- `skip_hashes`: pre-verification skip set for already-tested POV content hashes.
- `max_per_hash`: optional cap for files loaded from `pov_dir` (for example, `1` keeps one representative per hash).
- POV file enumeration from `pov_dir` is deterministic (sorted by filename) so capped selection is stable across runs.

### Patch Engine (`evaluation/verification/patch/engine.py`)

```
verify_benchmark()
  → ThreadPoolExecutor(build_workers)     # Level 1: parallel patches
      → verify_patch(patch)
          → build (apply patch, compile)
          → ThreadPoolExecutor(verify_workers)  # Level 2: parallel POV variants
              → reproduce(pov_variant)
          → run_unit_tests()
```

### Problems

1. Nested pools make total concurrency hard to reason about
2. `build_workers × verify_workers` can oversubscribe CPU/memory
3. No dependency tracking between jobs
4. Can't share builds between verify and patch-verify

## DAG Executor

Replace nested ThreadPoolExecutors with a single DAG executor.

### Job Primitives

```python
class Job:
    id: str
    depends_on: list[str]    # Job IDs that must complete first
    fn: Callable             # The actual work

# Atomic job types
build_image(project, mode)           → Docker image ready
verify_pov(project, pov, harness)    → pass/fail
build_patch(project, patch_id)       → patched variant ready
test_patch(project, patch_id, mode)  → pass/fail
collect_coverage(project)            → coverage data
```

### Single Executor

```python
class DAGExecutor:
    def __init__(self, max_workers: int):
        self.pool = ThreadPoolExecutor(max_workers)

    def execute(self, jobs: list[Job]) -> dict[str, JobResult]:
        # Topological sort → schedule ready jobs → collect results
        # One pool, one max_workers knob
```

### Benefits

- Single `--max-parallel` controls all concurrency
- Explicit dependencies prevent filesystem conflicts
- Same executor works for CI (many benchmarks) and standalone (single benchmark)
- Easy to add new job types without restructuring

### Migration Path

1. Build DAG executor as new module (`crsbench/executor/`)
2. CI commands adopt DAG executor first
3. Standalone commands migrate to DAG executor
4. Remove ThreadPoolExecutor usage from engines
