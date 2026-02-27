# Design Document: Unified Build & Verify Architecture

**Author**: CRSBench Development Team
**Date**: 2026-02-01
**Status**: Design
**References**: [distributed-job-queue.md](distributed-job-queue.md), [distributed-evaluation.md](distributed-evaluation.md)

> This document describes the unification of build and verify execution into a single Redis-centric architecture. It replaces DAGExecutor, ThreadPoolExecutor in the evaluator, and inline `verify_pov()` during CRS runs with one execution model: Redis queues processed by evaluator workers.

## 1. Motivation

The current codebase has three separate execution paths for builds and three for verification:

**Build paths:**
1. `DAGExecutor` + `BuildSingleVariantJob` (CI via `crsbench benchmark ci build` / `crsbench benchmark ci all`)
2. `ThreadPoolExecutor` in `evaluator.py:_build_all_variants()` (evaluator startup)
3. `VerificationEngine.get_or_build_results()` called inline by `BuildVariantsJob` (CI legacy path) and `POVVerificationManager._verify_pov()` (CRS run)

**Verify paths:**
1. `DAGExecutor` + `VerifyCpvPovJob` / `VerifyCpvVarJob` (CI)
2. `verify_povs()` RQ job in `evaluator_jobs.py` (distributed evaluator)
3. `POVVerificationManager._verify_pov()` inline during CRS trial (blocking the worker)

Each path has its own concurrency model, error handling, and caching strategy. This makes the system harder to reason about and maintain. The unified architecture collapses these into **one execution model**: Redis queues consumed by evaluator workers.

## 2. Architecture

### 2.1 Single Execution Model

Every command that needs builds or verification follows the same pattern:

```
Any command (ci build, ci all, run, evaluator)
  → VariantPlanner creates list[BuildSingleVariantJob]
  → Enqueue to Redis build queue
  → Evaluator workers execute builds
  → (if verify needed) Enqueue verify jobs to Redis verify queue
  → Evaluator workers execute verification
  → Orchestrator polls for results
```

### 2.2 Redis Required Always

Redis is the single execution backend. There is no fallback to local `ThreadPoolExecutor` or `DAGExecutor`. Even single-machine setups run a local Redis instance. This eliminates the need to maintain two code paths.

### 2.3 Queue Layout

| Queue | Name | Purpose |
|-------|------|---------|
| Build | `crsbench_build` (flat default) / `crsbench_{experiment}_build` (legacy) | Variant image builds |
| Verify | `crsbench_verify` (flat default) / `crsbench_{experiment}_verify` (legacy) | POV verification |
| Trial | `crsbench_trial` (flat default) / `crsbench_{experiment}` (legacy) | CRS trial execution |

Build and verify share the same evaluator worker pool. The evaluator dequeues from both build and verify queues, prioritizing build jobs (since verify jobs depend on completed builds).
Queue names are resolved by runtime queue model (`CRSBENCH_QUEUE_MODEL`), not hardcoded by consumers.

## 3. Two-Phase Execution for CI

### 3.1 `crsbench benchmark ci all`

CI execution uses two sequential phases with all jobs within each phase running in parallel:

```
Phase 1 (Build — all independent, all parallel):
  BuildSingleVariantJob  (vulnerable, allpatched, per-CPV, patched)
  Submit all → poll until all complete

Phase 2 (Verify/Test — all parallel):
  VerifyCpvPovJob, VerifyCpvVarJob, PatchPovTestJob,
  PatchVarTestJob, PatchUnitTestJob
  Submit all → poll until all complete
```

Phase 2 jobs only start after all Phase 1 builds complete. This is enforced by the orchestrator (poll loop), not by job-level dependencies. DAGExecutor's topological sort is replaced by this simpler two-phase barrier.

### 3.2 `crsbench benchmark ci build`

Same as Phase 1 above. `VariantPlanner` creates `BuildSingleVariantJob` instances, enqueues them, and waits for completion.

### 3.3 Phased Execution Implementation

```python
def run_ci_all(benchmarks, redis_conn, experiment_name):
    trial_q, build_q, verify_q = resolve_queue_names(experiment_name)
    build_queue = rq.Queue(build_q, connection=redis_conn)
    verify_queue = rq.Queue(verify_q, connection=redis_conn)

    # Phase 1: Enqueue all builds
    planner = VariantPlanner(benchmarks)
    build_jobs = planner.plan_all_builds()
    rq_build_jobs = [build_queue.enqueue(execute_build, job, result_ttl=-1) for job in build_jobs]
    poll_until_complete(rq_build_jobs)

    # Phase 2: Enqueue all verify/test jobs
    verify_jobs = planner.plan_all_verifications(build_results=collect_results(rq_build_jobs))
    rq_verify_jobs = [verify_queue.enqueue(execute_verify, job, result_ttl=-1) for job in verify_jobs]
    poll_until_complete(rq_verify_jobs)
```

## 4. VariantPlanner

### 4.1 Purpose

`VariantPlanner` centralizes build job creation. Currently, build jobs are created in at least four locations:
- `crsbench/benchmark_ci/cli/commands/all_cmd.py` (CI all)
- `crsbench/benchmark_ci/cli/commands/build_cmd.py` (CI build)
- `crsbench/distributed/evaluator.py:_build_all_variants()` (evaluator startup)
- `crsbench/evaluation/verification/pov/engine.py:get_or_build_results()` (inline during CRS run)

`VariantPlanner` replaces all of these with a single function: given a benchmark, produce the list of `BuildSingleVariantJob` instances needed.

### 4.2 Location

`crsbench/executor/variant_planner.py`

### 4.3 Interface

```python
class VariantPlanner:
    """Converts benchmark metadata into BuildSingleVariantJob lists."""

    def __init__(self, benchmarks_root: Path, oss_fuzz_path: Path):
        ...

    def plan_builds(self, benchmark_path: Path) -> list[BuildSingleVariantJob]:
        """Create build jobs for all variants of a single benchmark.

        Includes: vulnerable, allpatched, per-CPV, and patched variants.
        All jobs are independent (no inter-job dependencies).
        """
        ...

    def plan_all_builds(self, benchmark_paths: list[Path]) -> list[BuildSingleVariantJob]:
        """Create build jobs for multiple benchmarks."""
        jobs = []
        for path in benchmark_paths:
            jobs.extend(self.plan_builds(path))
        return jobs
```

### 4.4 Key Design Decision: Patch Builds Are Independent

Patched variant builds do **not** depend on the vulnerable variant build. All builds are Phase 1 peers. This is possible because `OSSFuzzBuilder.build_single()` is self-contained — it fetches source, applies patches, and builds independently.

## 5. BuildSingleVariantJob as Universal Primitive

### 5.1 Absorbing BuildPatchVariantJob

The current `BuildPatchVariantJob` (in `crsbench/benchmark_ci/jobs/flat.py:692-853`) is a separate class that handles patched variant builds through `PatchVerificationEngine`. This is unnecessary because `BuildSingleVariantJob` already supports `variant_type=VariantType.PATCHED` via `OSSFuzzBuilder.build_single()`.

The merge eliminates:
- A second build job class with its own error handling
- The dependency of `BuildPatchVariantJob` on a prior `BuildVariantsJob` (via `build_job_id`)
- The `PatchVerificationEngine(build_only=True)` workaround

After the merge, `BuildSingleVariantJob` handles all variant types including `PATCHED`. The `patches` field (already present on `BuildSingleVariantJob`) carries the patch file path. The `cpv_num` field identifies the CPV.

### 5.2 Fields Added to BuildSingleVariantJob

No new fields are needed. The existing `BuildSingleVariantJob` (in `crsbench/benchmark_ci/jobs/flat.py:49-191`) already has:
- `variant_type: VariantType` — supports `PATCHED`
- `patches: list[Path]` — carries patch files
- `cpv_num: Optional[int]` — identifies the CPV
- `sanitizer: str` — per-CPV sanitizer

## 6. BuildResultCache

### 6.1 Problem

Build results are currently cached in two ways:
- Module-level globals in `evaluator_jobs.py`: `_built_results` and `_verification_engine` (set via `set_build_cache()` at line 180-192)
- `VerificationEngine._built_results` (instance-level cache on the engine)

Module-level globals are fragile (not thread-safe, hard to test, process-scoped).

### 6.2 Replacement

`BuildResultCache` is a thread-safe class that stores build results keyed by `(benchmark_name, variant_name)`.

### 6.3 Location

`crsbench/executor/build_cache.py`

### 6.4 Interface

```python
class BuildResultCache:
    """Thread-safe cache for build results."""

    def get(self, benchmark: str, variant: str) -> Optional[BuildResult]:
        ...

    def put(self, benchmark: str, variant: str, result: BuildResult) -> None:
        ...

    def get_benchmark(self, benchmark: str) -> dict[str, BuildResult]:
        """Get all build results for a benchmark."""
        ...

    def has_benchmark(self, benchmark: str) -> bool:
        ...
```

The cache is passed explicitly to evaluator job functions and to `VariantPlanner`, replacing the `set_build_cache()` global setter pattern.

## 7. Per-POV Verify Jobs

### 7.1 Current: Batch verify_povs()

The current `verify_povs()` in `evaluator_jobs.py:195-332` receives a `VerificationJobPayload` containing a list of POVs and verifies them sequentially in a single RQ job. This couples all POVs from one snapshot into one job.

### 7.2 New: verify_single_pov()

Split into per-POV jobs for finer granularity:

```python
def verify_single_pov(payload_dict: dict[str, Any]) -> dict[str, Any]:
    """Verify a single POV. RQ job function.

    Args:
        payload_dict: Serialized SinglePovPayload

    Returns:
        Serialized PovVerdict
    """
```

Benefits:
- Individual POVs can be retried without re-verifying the batch
- Better parallelism — evaluator workers process POVs concurrently
- Smaller job payloads in Redis
- Clearer failure isolation

The `VerificationJobPayload` is replaced by a simpler `SinglePovPayload` containing one `EmbeddedPov`.

## 8. Async POV Verification During CRS Run

### 8.1 Current: Inline Verification

`POVVerificationManager._verify_pov()` (in `crsbench/evaluation/verification/pov/manager.py:199-232`) calls `VerificationEngine.verify_pov()` synchronously during `on_snapshot()`. This blocks the snapshot cycle while variant builds and reproduce() execute on the worker machine.

### 8.2 New: Enqueue → Poll → Early-Stop

```
CRS worker discovers POVs in on_snapshot()
  → POVVerificationManager reads POV bytes
  → Enqueues VerifySinglePovJob to Redis verify queue per POV
  → Stores RQ job IDs in trial metadata
  → Returns from on_snapshot() immediately (non-blocking)

Every ~60s (configurable poll interval):
  → POVVerificationManager polls RQ job results for pending verify IDs
  → Updates found_cpvs set from completed verdicts
  → If all_cpvs_found → signals early-stop event
```

### 8.3 Acceptable Delay

The 60-second poll interval introduces a 1-2 minute delay before early-stop triggers (poll interval + evaluator processing time). This is acceptable because:
- CRS trials typically run for hours (3600s+)
- Early-stop saves the remaining trial time, not the first few minutes
- The delay is bounded and predictable

### 8.4 Graceful Degradation

If no evaluator is running, verify jobs accumulate in Redis. The worker continues normally — POV verification is fully decoupled from trial execution. An evaluator can drain the queue post-experiment.

## 9. Changes Per Component

### 9.1 `crsbench benchmark ci build`

**Before:** Creates `BuildSingleVariantJob` list, executes via `DAGExecutor`.
**After:** Creates `BuildSingleVariantJob` list via `VariantPlanner`, enqueues to Redis build queue, polls for completion.

### 9.2 `crsbench benchmark ci all`

**Before:** Creates full DAG (build + verify + patch-build + patch-test jobs), executes via `DAGExecutor` with topological ordering and `type_limits`.
**After:** Phase 1: `VariantPlanner.plan_all_builds()` → enqueue → wait. Phase 2: plan verify/test jobs from build results → enqueue → wait.

### 9.3 `crsbench run` (Orchestrator)

**Before:** Builds variants locally via `VerificationEngine.get_or_build_results()`, then enqueues CRS trial jobs.
**After:** `VariantPlanner.plan_all_builds()` → enqueue to Redis build queue → wait for builds → enqueue CRS trial jobs to trial queue.

### 9.4 `crsbench evaluator`

**Before:** Builds all variants at startup via `ThreadPoolExecutor`, then listens only on verify queue.
**After:** Listens on both build queue and verify queue. In configless mode, builds arrive lazily as jobs; in config-pinned mode, startup pre-build enqueue may be used. The evaluator is a generic job worker for build and verify queues.

### 9.5 `crsbench worker` (CRS trial execution)

**Before:** `POVVerificationManager._verify_pov()` calls `VerificationEngine.verify_pov()` inline (blocking).
**After:** `POVVerificationManager` enqueues `VerifySinglePovJob` to Redis verify queue, polls for results every ~60s.

## 10. Deprecations and Migration Path

### 10.1 Deprecated Components

| Component | File | Replacement |
|-----------|------|-------------|
| `DAGExecutor` | `crsbench/executor/dag.py` | Redis phased execution (enqueue → poll) |
| `BuildVariantsJob` | `crsbench/benchmark_ci/jobs/flat.py:194-307` | `BuildSingleVariantJob` via `VariantPlanner` |
| `BuildPatchVariantJob` | `crsbench/benchmark_ci/jobs/flat.py:692-853` | `BuildSingleVariantJob` with `variant_type=PATCHED` |
| `ThreadPoolExecutor` in evaluator | `crsbench/distributed/evaluator.py:116,152` | Redis build queue |
| `set_build_cache()` module globals | `crsbench/distributed/evaluator_jobs.py:180-192` | `BuildResultCache` passed explicitly |
| `VerificationEngine.get_or_build_results()` as primary build path | `crsbench/evaluation/verification/pov/engine.py:759-816` | `BuildSingleVariantJob` via Redis |
| Inline `verify_pov()` during CRS run | `crsbench/evaluation/verification/pov/manager.py:199-232` | Async Redis verify + poll |
| Batch `verify_povs()` RQ job | `crsbench/distributed/evaluator_jobs.py:195-332` | Per-POV `verify_single_pov()` |

### 10.2 Preserved Components

These components remain unchanged:
- `BuildSingleVariantJob` — becomes the universal build primitive
- `VerifyCpvPovJob`, `VerifyCpvVarJob` — reused as verify job payloads for CI
- `PatchPovTestJob`, `PatchVarTestJob`, `PatchUnitTestJob` — reused as verify job payloads for CI
- `OSSFuzzBuilder.build_single()` — still the actual build executor inside `BuildSingleVariantJob`
- `VerificationEngine.verify_pov()` — still the actual verification logic, called by evaluator job functions
- `POVVerificationManager` — modified to enqueue instead of verify inline, but class structure preserved
- RQ/Redis infrastructure — same queue library, same job result format

### 10.3 Migration Strategy

1. Implement `VariantPlanner` and `BuildResultCache` as new modules
2. Add `verify_single_pov()` alongside existing `verify_povs()` in `evaluator_jobs.py`
3. Modify evaluator to listen on both build and verify queues
4. Modify CI commands to use Redis instead of `DAGExecutor`
5. Modify `POVVerificationManager` to enqueue + poll instead of inline verify
6. Remove deprecated components after all callers are migrated

### 10.4 New File Locations

```
crsbench/executor/
├── variant_planner.py      # NEW: benchmark → list[BuildSingleVariantJob]
├── build_cache.py           # NEW: thread-safe build result cache
├── dag.py                   # DEPRECATED: DAGExecutor
├── errors.py                # Kept (CycleError, DependencyError — may be removed later)
└── types.py                 # Kept (ExecutorResult, JobStatus)

crsbench/distributed/
├── evaluator.py             # MODIFIED: listen on build + verify queues
├── evaluator_jobs.py        # MODIFIED: add verify_single_pov(), deprecate verify_povs()
├── jobs.py                  # Existing (CRS trial jobs)
├── worker.py                # Existing (CRS trial workers)
└── queue.py                 # MODIFIED: add build queue helpers
```

---

**Document Version**: 1.0
**Last Updated**: 2026-02-01
