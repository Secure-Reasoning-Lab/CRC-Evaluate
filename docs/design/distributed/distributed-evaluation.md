# Design Document: Distributed Evaluation for CRSBench

**Author**: CRSBench Development Team
**Date**: 2026-02-01
**Status**: Design
**Reference**: [distributed-job-queue.md](distributed-job-queue.md) for existing infrastructure

> This document extends the existing distributed job queue architecture with a third process type -- the **evaluator** -- that decouples POV verification from CRS trial execution. Read `distributed-job-queue.md` first for the base architecture (orchestrator + workers + Redis/RQ).

## 1. Overview

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

**File:** `crsbench/distributed/cli/evaluator_command.py`

```python
def add_evaluator_subparser(subparsers) -> None:
    evaluator_parser = subparsers.add_parser(
        "evaluator",
        help="Run distributed evaluator to build variants and verify POVs",
    )

    evaluator_parser.add_argument(
        "--experiment-config", type=str, required=True,
        help="Path to experiment configuration YAML file",
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

1. Load experiment config from `--experiment-config`
2. Parse benchmark list from config (scoped to experiment)
3. Build ALL variant Docker images for listed benchmarks using `OSSFuzzBuilder`
4. Cache build results in `VerificationEngine._built_results` keyed by benchmark name
5. Connect to Redis, start listening on `crsbench_{exp}_verify` queue

Building at startup ensures variants are ready before any verify job arrives.

### 3.3 Supervisor Pattern

**File:** `crsbench/distributed/evaluator.py`

The evaluator supervisor mirrors `worker.py _run_supervisor()`:

```python
def _run_evaluator_supervisor(redis_host, experiment_name, max_jobs, ...):
    cpu_pool = CPUPool() if use_cpuset else None
    verify_queue = rq.Queue(
        f"crsbench_{experiment_name}_verify",
        connection=redis_conn,
    )

    while True:
        # Cleanup finished workers
        # ...

        # Dequeue verify job
        result = rq.Queue.dequeue_any(
            [verify_queue], timeout=None, connection=redis_conn,
        )
        if result:
            job, _ = result
            cpus = cpu_pool.allocate(cpu_count) if cpu_pool else None
            # Spawn child process to run verification
            p = multiprocessing.Process(
                target=_run_single_verify_job,
                args=(redis_host, experiment_name, name, job.id),
            )
            p.start()
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
| Trial queue | `crsbench_{experiment_name}` | Workers | CRS trial execution |
| Verify queue | `crsbench_{experiment_name}_verify` | Evaluators | POV verification |

Both queues use the same Redis/Valkey instance. Multiple evaluators can share the same verify queue (RQ handles distribution).

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

The evaluator builds ALL variant images at startup before listening on the verify queue:

1. Load experiment config to determine benchmark list
2. For each benchmark, use `OSSFuzzBuilder.create_build_plan()` + `execute_plan()`
3. Cache build results in `VerificationEngine._built_results` keyed by benchmark name
4. Only after all builds complete does the evaluator start consuming from the queue

### 8.2 Shared Build Infrastructure

The evaluator uses the same build pipeline as `crsbench benchmark ci build`:
- `OSSFuzzBuilder` for build plan creation and execution
- `OSSFuzzInfrastructure.reproduce()` for POV verification
- Build results cached identically to `VerificationEngine._get_or_build_results()`

### 8.3 Unknown Benchmark Handling

If a verify job arrives for a benchmark not in the experiment config (and therefore not pre-built), the evaluator logs an error and fails the job. The worker sees the failure when polling.

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

**Implementation note:** The current codebase does NOT pass cgroup constraints to Docker containers spawned by `helper.py`. This should be resolved in Phase 20 implementation by passing `--cgroup-parent` to `docker run` calls made during `reproduce()`. The worker already has `_setup_shared_cgroup` (in `worker.py`) that provides the pattern.

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

The evaluator reads paths from the experiment config YAML:

| Field | Purpose |
|-------|---------|
| `oss_fuzz_path` | Path to oss-fuzz checkout |
| `benchmarks_root` | Path to benchmarks directory |

### 11.2 Experiment Config

No new fields needed in `ExperimentConfig`. The evaluator reads the same config as workers to determine:
- Which benchmarks to build variants for
- `max_total_time` for job timeout
- Build settings (sanitizers, incremental build, etc.)

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

## 13. Future Enhancements

- **Lazy variant building:** Build on first verify job for a benchmark instead of all at startup
- **Evaluator health endpoint:** HTTP endpoint for monitoring evaluator status and build cache
- **Result streaming:** Push results to orchestrator via Redis pub/sub instead of worker polling

---

**Document Version**: 1.0
**Last Updated**: 2026-02-01
**Next Review**: After Phase 20 implementation
