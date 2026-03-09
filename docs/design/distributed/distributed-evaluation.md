# Design Document: Distributed Evaluation for CRSBench

**Author**: CRSBench Development Team
**Date**: 2026-02-01
**Status**: Design
**Reference**: [distributed-job-queue.md](distributed-job-queue.md) for existing infrastructure

> This document extends the existing distributed job queue architecture with a third process type -- the **evaluator** -- that decouples POV verification from CRS trial execution. Read `distributed-job-queue.md` first for the base architecture (orchestrator + workers + Redis/RQ).

## 1. Overview

### 1.3 Current Implementation Notes (2026-03)

The evaluator has three runtime modes in the current implementation:

- Configless (default): no `--experiment-config`, no `--ci`
- Config-pinned: `--experiment-config <yaml>`
- CI compatibility alias: `--ci` (uses unified configless evaluator path with legacy queues `crsbench_ci_build` + `crsbench_ci_verify`)

In configless mode, evaluator behavior is:

1. Poll Redis registry until at least one experiment is registered.
2. Discover build/verify queues from registry entries.
3. Start multi-queue supervisor and process build + verify queues.

Important constraints for configless multi-experiment operation:

- All discovered experiments must have the same `benchmarks_root`.
- All discovered experiments must have compatible inc-image settings
  (`inc_image_policy`, `inc_image_registry`, max pull bytes, pull timeout, and image prefix).
- Evaluator refreshes discovered build/verify queue sets periodically at runtime
  (registry-driven queue refresh).
- Verifier timeout is shared as the max `per_pov_verify_timeout` across discovered experiments.

CI hardening behavior (commit `a1c9b038`, 2026-03-06):

- CI DAG enqueue now validates dependencies strictly: unknown dependency IDs and out-of-order dependencies fail fast.
- Duplicate IDs are handled by deterministic stale-job policy:
  - `finished`/`failed`/`stopped`/`canceled` refreshed by default (`refresh_all`)
  - build jobs with `finished` are always refreshed
  - non-build `finished` jobs are reused only with `refresh_stopped_canceled_failed`
  - `queued`/`deferred`/`scheduled` always refreshed
  - active non-terminal jobs are reused
- Terminal statuses are handled uniformly as done states: `finished`, `failed`, `stopped`, `canceled`.
- If `rq.job.Job.fetch_many()` returns `None` for a pending job, the result is recorded as infrastructure failure with `error_code=infra_missing_rq_job`.
- If a job remains `started` beyond timeout+grace, it is recorded as `infra_stale_started_job` (retryable).

Patch verification runtime source contract:

- CRSBench mounts benchmark metadata/scripts at `/CRSBENCH_PROJ_PATH` and patched source at `/CRSBENCH_PATCHED_SRC`.
- Unit-test execution resolves the effective container `WORKDIR` from image metadata (fallback: benchmark Dockerfile parsing).
- Patched source is synchronized into that resolved `WORKDIR` (rsync `--delete` semantics), then `test.sh`/`run_tests.sh` executes from that directory.
- `/src` is not treated as a universal source root during patch verification.

### 1.1 Goals

- Decouple POV verification (variant building + reproduce) from CRS trial execution
- Enable multi-machine evaluation where workers and evaluators run on separate hardware
- Support offline post-experiment verification (drain queued verify jobs after trials complete)
- Free worker CPU for CRS execution by offloading heavyweight variant builds to evaluators

### 1.2 Non-Goals

- Replacing the existing worker architecture (workers remain unchanged for trial execution)
- Auto-scaling evaluators based on queue depth
- Real-time streaming of verification results to the orchestrator
- Sharing Docker images between machines (each evaluator builds locally)

## 2. Architecture Overview

### 2.1 Three-Process Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                     CRSBench Experiment                          │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
         ┌────────────────────────────────────────┐
         │   Orchestrator (crsbench run)          │
         │                                        │
         │  • Parse CLI, validate config          │
         │  • Build variant images (local)        │
         │  • Generate trial matrix               │
         │  • Enqueue CRS trial jobs              │
         │  • Monitor progress                    │
         └────────┬───────────────────────────────┘
                  │
                  │ RQ Job Enqueue
                  ▼
           ┌──────────────┐
           │ Redis/Valkey  │
           │               │
           │ Trial queue:  │  crsbench_{exp}
           │ Verify queue: │  crsbench_{exp}_verify
           │ Job results   │
           └──┬─────────┬──┘
              │         │
     Trial    │         │  Verify
     jobs     │         │  jobs
              ▼         ▼
    ┌──────────────┐  ┌──────────────────────────┐
    │ Workers × N  │  │ Evaluators × M           │
    │              │  │                           │
    │ • Run CRS    │  │ • Build variant images    │
    │ • Discover   │──│ • Dequeue verify jobs     │
    │   POVs       │  │ • Run reproduce()         │
    │ • Enqueue    │  │ • Store verdicts in Redis │
    │   verify job │  │                           │
    │ • Poll for   │  │ (crsbench evaluator)      │
    │   results    │  │                           │
    └──────────────┘  └──────────────────────────┘
    (crsbench worker)
```

**Data flow:**
1. Worker runs CRS trial, discovers POV files in `trial_output_dir/output/povs`
2. Worker reads POV bytes, enqueues `VerificationJobPayload` to verify queue
3. Evaluator dequeues job, runs `VerificationEngine.verify_pov()` against pre-built variants
4. Evaluator stores verdict as RQ job result
5. Worker polls verify job IDs periodically, extracts verdicts

### 2.2 Technology Stack

Same as existing infrastructure:
- **Valkey 8.0+** / Redis: Message broker and job storage
- **RQ 1.11.1+**: Python job queue (works with Valkey)
- **Python 3.11+**: Implementation language
- **Docker**: Variant image builds and reproduce() execution

## 3. Evaluator Component Design

### 3.1 CLI Interface

**Command:** `crsbench evaluator`

Follows the `add_evaluator_subparser()` pattern from `worker_command.py`.
`--experiment-config` is optional (configless discovery is the default when omitted).

**File:** `crsbench/distributed/cli/evaluator_command.py`

```python
def add_evaluator_subparser(subparsers) -> None:
    evaluator_parser = subparsers.add_parser(
        "evaluator",
        help="Run distributed evaluator to build variants and verify POVs",
    )

    evaluator_parser.add_argument(
        "--experiment-config", type=str, required=False, default=None,
        help="Focus on a specific experiment (default: discover all from registry)",
    )
    evaluator_parser.add_argument(
        "--build-jobs", type=int, default=1,
        help="Max concurrent build jobs (default: 1)",
    )
    evaluator_parser.add_argument(
        "--verify-jobs", type=int, default=None,
        help="Max concurrent verify jobs (default: build-jobs)",
    )
    evaluator_parser.add_argument(
        "--benchmarks-root", type=str, default=None,
        help="Override benchmarks root directory",
    )

    evaluator_parser.set_defaults(command="evaluator")
```

**Registration** in `run_experiment.py`:
```python
from crsbench.distributed.cli.evaluator_command import add_evaluator_subparser
add_evaluator_subparser(subparsers)
```

### 3.2 Startup Sequence

Config-pinned mode (`--experiment-config`):

1. Load experiment config from `--experiment-config`
2. Resolve queues for that experiment
3. Enqueue pre-build jobs for experiment benchmarks
4. Start dual-queue supervisor (`build` + `verify`)

Configless mode (default):

1. Poll registry until experiments are available
2. Resolve queue lists from registry (`*_build`, `*_verify`)
3. Start multi-queue supervisor over discovered queues

Build activity happens lazily as workers enqueue build jobs; configless startup does not trigger an eager pre-build phase.

### 3.3 Supervisor Pattern

**File:** `crsbench/distributed/evaluator.py`

The evaluator supervisor uses a dual/multi-queue pattern with build-priority dequeue:

```python
def run_multi_queue_supervisor(...):
    while True:
        # 1) Reap finished workers and release CPU/cgroup resources
        # 2) Refresh queue set from registry periodically (configless mode)
        # 3) Build-priority dequeue from queues that currently have capacity
        # 4) Apply cpu_tag filtering / requeue on mismatch
        # 5) Spawn child process for one job (build or verify)
```

### 3.4 File Locations

```
crsbench/distributed/
├── cli/
│   ├── worker_command.py       # Existing
│   └── evaluator_command.py    # NEW
├── evaluator.py                # NEW: evaluator process
├── evaluator_jobs.py           # NEW: verify job execution
├── worker.py                   # Existing (unchanged)
├── jobs.py                     # Existing (add verify enqueue)
└── queue.py                    # Existing (extend for verify queue)
```

## 4. Verification Job Schema

### 4.1 Job Payload

```python
@dataclass
class VerificationJobPayload:
    experiment_name: str        # For queue routing
    trial_id: str               # Correlate results back to trial
    benchmark: str              # Benchmark name (evaluator resolves path locally)
    harness: str                # Fuzz harness name
    povs: list[EmbeddedPov]     # List of POVs from this trial snapshot
    enqueued_at: float          # Timestamp

@dataclass
class EmbeddedPov:
    pov_id: str                 # Filename/identifier
    pov_data: bytes             # Raw POV file content (base64 encoded in Redis)
```

### 4.2 Why Embedded POV Data

POV data is embedded in the job payload (not referenced by path) because workers and evaluators may run on different machines with no shared filesystem. This is a locked design decision.

**Practical limits:**
- POVs are typically < 1MB (fuzzer-generated test cases)
- Worker should warn and skip POVs > 10MB
- Redis handles this size well with `result_ttl=-1`

### 4.3 Batch Granularity

Jobs are batched per-trial: all POVs discovered in one snapshot cycle are bundled into one verification job. This reduces Redis overhead while keeping jobs self-contained.

## 5. Redis Queue Design

### 5.1 Queue Naming

| Queue | Name | Consumers | Purpose |
|-------|------|-----------|---------|
| Trial queue | `crsbench_trial` (flat default) / `crsbench_{experiment_name}` (legacy) | Workers | CRS trial execution |
| Verify queue | `crsbench_verify` (flat default) / `crsbench_{experiment_name}_verify` (legacy) | Evaluators | POV verification |

Both queues use the same Redis/Valkey instance. Multiple evaluators can share the same verify queue (RQ handles distribution).
Queue names are resolved via runtime queue model (`CRSBENCH_QUEUE_MODEL`), not hardcoded by consumers.

### 5.2 Job Configuration

- **Job timeout:** Configurable, default to `max_total_time` from experiment config
- **Result TTL:** `-1` (persist forever, same as trial jobs)
- **Enqueue function:** `crsbench.distributed.evaluator_jobs.verify_povs`

```python
verify_queue.enqueue(
    "crsbench.distributed.evaluator_jobs.verify_povs",
    payload=payload.to_dict(),
    job_timeout=config.max_total_time,
    result_ttl=-1,
)
```

## 6. Result Storage Format

### 6.1 Storage Mechanism

Use RQ native job results (not separate Redis keys). Each completed verify job stores its result as the RQ job's return value.

### 6.2 Result Schema

```python
@dataclass
class VerificationResult:
    trial_id: str
    benchmark: str
    harness: str
    verdicts: list[PovVerdict]
    completed_at: float

@dataclass
class PovVerdict:
    pov_id: str
    triggered_bug: bool
    cpv_matches: list[str]      # Which CPVs this POV matches (if any)
    variant_results: dict[str, bool]  # variant_name -> crash/no-crash
    error: Optional[str]
```

### 6.3 How Workers Retrieve Results

Workers track verify job IDs in their own RQ job meta:

```python
# Worker side: after enqueue
job = rq.get_current_job()
if job:
    verify_ids = job.meta.get("verify_job_ids", [])
    verify_ids.append(verify_job.id)
    job.meta["verify_job_ids"] = verify_ids
    job.save_meta()
```

Workers poll periodically:
```python
# Worker side: during trial (every 60s)
for job_id in job.meta.get("verify_job_ids", []):
    verify_job = rq.job.Job.fetch(job_id, connection=redis_conn)
    if verify_job.result is not None:
        result = verify_job.result  # VerificationResult
        # Update local POV store, check early termination
```

## 7. Worker Integration (Async POV Enqueue)

### 7.1 Current Flow (Replaced)

```
run_crs_trial() -> BenchmarkRunner.run_benchmark()
  -> _verify_povs() (inline, blocks worker)
    -> VerificationEngine.verify_benchmark()
      -> build variants (if not cached)
      -> reproduce() for each (pov, variant) pair
```

### 7.2 New Flow

```
run_crs_trial() -> discovers POVs in trial_output_dir/output/povs
  -> reads POV bytes
  -> enqueues VerificationJobPayload to verify queue
  -> stores verify job ID in own job meta
  -> continues trial execution (non-blocking)
```

### 7.3 Polling and Early Termination

- Worker polls for results at configurable interval (default 60s) during trial
- If all CPVs found via async results, worker can trigger early termination
- Fire-and-forget: Worker always enqueues, regardless of evaluator presence

## 8. Build-Before-Verify Ordering

### 8.1 Evaluator Build Strategy

Current behavior in configless mode uses queue-driven lazy builds:

1. Discover build/verify queues from registry.
2. Start supervisor immediately over build + verify queues.
3. Consume build jobs lazily from build queues as workers enqueue them.
4. Build jobs execute with queue priority before verify jobs when both are pending.

Config-pinned mode (`--experiment-config`) may still enqueue startup pre-builds for its single target experiment.

### 8.2 Shared Build Infrastructure

The evaluator uses the same build pipeline as `crsbench benchmark ci build`:
- `OSSFuzzBuilder` for build plan creation and execution
- `OSSFuzzInfrastructure.reproduce()` for POV verification
- Build results cached identically to `VerificationEngine._get_or_build_results()`

### 8.3 Unknown Benchmark Handling

If a verify job arrives before required builds are available, it remains queued until its corresponding build jobs complete; build queues are prioritized over verify queues.

## 9. CPU / cgroup Allocation

### 9.1 CPU Pool

The evaluator supervisor allocates CPUs from `CPUPool` per verify job, same pattern as the worker supervisor:

```python
cpu_pool = CPUPool()
cpus = cpu_pool.allocate(cpu_count)
if cpus:
    cpuset_str = format_cpuset(cpus)
    job.meta["allocated_cpus"] = cpuset_str
    job.save_meta()
```

### 9.2 Docker Container Constraints

For the DooD (Docker-outside-of-Docker) pattern used by OSS-Fuzz: use `--cgroup-parent` Docker flag to nest spawned containers under the evaluator's cgroup.

**Implementation note:** Current code propagates cgroup/cpuset constraints to OSS-Fuzz helper flows via env (`OSS_FUZZ_CGROUP_PARENT`, `OSS_FUZZ_CPUSET_CPUS`) and corresponding helper integration.

## 10. Graceful Degradation

### 10.1 Without Evaluator

- Verify jobs accumulate in Redis without errors on the worker side
- Workers continue trial execution normally -- verification is decoupled
- Redis memory impact is minimal (POVs are small, jobs persist with `result_ttl=-1`)

### 10.2 Post-Experiment Evaluation

After an experiment completes:
1. Master syncs experiment data from workers
2. Operator runs `crsbench evaluator` offline to drain the verify queue
3. Evaluator builds variants, processes all queued verify jobs, stores verdicts
4. Results can be collected from Redis by re-running the orchestrator in analysis mode

### 10.3 Optional Skip

Worker config flag `skip_verification: true` skips inline verification entirely when an evaluator is expected. Workers enqueue verify jobs regardless of this flag.

## 11. Configuration

### 11.1 Evaluator Config

Config-pinned mode (`--experiment-config`) reads paths from that YAML:

| Field | Purpose |
|-------|---------|
| `benchmarks_root` | Path to benchmarks directory |

### 11.2 Experiment Config

Configless mode reads runtime registration from Redis (published by orchestrator) to determine:

- Queue names (`trial`, `build`, `verify`)
- Benchmark list and mode metadata
- `benchmarks_root`
- Per-experiment verify timeout metadata

`ExperimentConfig` now supports an optional `evaluator:` block. In configless mode,
the orchestrator publishes `evaluator.*` runtime hints into the registry, and the
evaluator resolves resources with precedence `CLI > registry metadata > defaults`.

### 11.3 Evaluator-Specific CLI Args

`--build-jobs` and `--verify-jobs` control parallel build and verify job counts respectively. These map to the evaluator supervisor's concurrency parameters.

## 12. Error Handling

| Scenario | Behavior |
|----------|----------|
| Build failure | Evaluator logs error, marks affected benchmarks as unbuildable, rejects verify jobs for those benchmarks |
| Verify failure | Job marked as failed in RQ, worker sees failure when polling |
| Redis disconnect | Evaluator retries connection (same pattern as worker) |
| Timeout | Verify job killed after configured timeout, marked as failed |
| Unknown benchmark | Job failed with error message, worker sees failure when polling |
| CI unknown/unresolved dependency | Enqueue fails fast with validation error (invalid DAG dependency definition/order) |
| CI duplicate job ID | Deterministic policy: terminal statuses refreshed by default (`refresh_all`); build `finished` always refreshed; non-build `finished` reused only with `refresh_stopped_canceled_failed`; queued/deferred/scheduled refreshed; active non-terminal reused |
| CI missing RQ job metadata during polling | Recorded as infrastructure failure with `error_code=infra_missing_rq_job` |
| CI stale started job during polling | Recorded as infrastructure failure with `error_code=infra_stale_started_job` (retryable) |
| Supervisor spawn/cgroup path transient failure | Job is re-enqueued and supervisor continues (no global crash) |

## 13. Future Enhancements

- **Evaluator health endpoint:** HTTP endpoint for monitoring evaluator status and build cache
- **Result streaming:** Push results to orchestrator via Redis pub/sub instead of worker polling

---

**Document Version**: 1.2
**Last Updated**: 2026-03-06
**Next Review**: After Phase 20 implementation
