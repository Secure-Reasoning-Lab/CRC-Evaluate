# Build-Verify Architecture

## Overview

This document defines how builds and verification work across all CRSBench commands. The core principle: **inc-build by default, full build as fallback, cache by default**.

## Build Modes

### Inc-Build (Default)

Uses pre-built Docker images (`ghcr.io/team-atlanta/crsbench/{project}:inc-{sanitizer}`) with pre-compiled dependencies. Only recompiles changed files.

### Full Build (Fallback)

Builds everything from scratch using base OSS-Fuzz images. Used when:
- Inc-build image not available
- Inc-build fails (automatic fallback → PASS-FB)
- User explicitly requests via `--no-inc-build`

### Cache

Builds are cached in `oss-fuzz/build/out/{variant_name}/`. By default, if a cached build exists and matches the requested mode, it's reused.

## Command Defaults

| Command | Build Mode | Cache | Force Rebuild | Fallback |
|---------|-----------|-------|---------------|----------|
| `crsbench verify` | inc-build | ON | `--force-rebuild` | inc→full |
| `crsbench patch-verify` | inc-build | ON | `--force-rebuild` | inc→full |
| `crsbench coverage` | inc-build | ON | `--force-rebuild` | inc→full |
| `crsbench run` | inc-build | ON | `--force-rebuild` | inc→full |
| `crsbench ci *` | inc-build | **OFF** | always | inc→full (PASS-FB) |

All commands support `--no-inc-build` to force full build mode.

### Standalone Commands

```
--no-inc-build    Use full build instead of inc-build
--force-rebuild   Ignore cache, rebuild from scratch
```

Default behavior:
```
Has cached build? ──yes──→ Use cached build
       │no
       ▼
Inc-image available? ──yes──→ Build with inc-image
       │no                          │
       ▼                       Build failed?
Full build (slow)                   │yes
                                    ▼
                              Fallback to full build (PASS-FB)
```

### CI Commands

CI always uses `force_rebuild=True` because it validates that the build process itself works.

CI defaults to inc-build (same as standalone). To test full build mode, use `--no-inc-build`. To compare both modes, run CI twice:
```bash
# Test with inc-build (default)
crsbench ci all --all --output-dir results-inc

# Test with full build
crsbench ci all --all --no-inc-build --output-dir results-full

# Compare results externally
diff results-inc/summary.json results-full/summary.json
```

## CI DAG Architecture

### Single-Mode Execution

Each CI invocation runs **one build mode** (inc-build by default). All checks fan out in parallel after the initial build.

```
START ── build ──┬── verify-pov(pov1)
                 ├── verify-pov(pov2)
                 ├── build-patch(p1) ── test(p1, full)
                 │                   └── test(p1, rts)
                 ├── build-patch(p2) ── test(p2, full)
                 │                   └── test(p2, rts)
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
- `test(p1, full)` and `test(p1, rts)` can run concurrently (same build, different test selection)
- `collect-coverage` runs concurrently with verify/patch jobs

### RTS

RTS runs alongside patch tests on the same build. It compares test selection strategies (full test.sh vs RTS-selected tests) — the build mode is fixed, only test selection differs.

### Removed CI Subcommands and Flags

- **`ci inc-build`**: Removed — redundant since `ci all` defaults to inc-build
- **`--check-inc` on `ci pov`/`ci patch`**: Removed — use `ci all` (inc-build default) or `--no-inc-build`
- **`--check-rts` on `ci patch`**: Removed — RTS always runs as part of `ci all`

## Timing Breakdown

Each check type reports timing differently based on what's being measured:

| Check | Timing | Rationale |
|-------|--------|-----------|
| POV | `B:Xs V:Ys` | Build and verify are separate phases |
| Patch | Total (e.g., `30s`) | Rebuild is part of verification (apply patch → rebuild → test) |
| Coverage | `B:Xs V:Ys` | Build and collection are separate phases |
| RTS | Total per-patch | Comparable with Patch timing (same build, different tests) |

### Per-Patch Detail

Patch and RTS checks include per-patch breakdown in `details`:
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
