# Configless Worker/Evaluator Runtime

## Overview

Workers and evaluators support two modes:

1. **Configless (default)**: Boot with only a Redis connection. The
   orchestrator (`crsbench run`) publishes experiment metadata to a **Redis
   experiment registry** before enqueueing jobs. Workers and evaluators
   discover experiments from the registry and serve multiple experiments
   concurrently.

2. **Config mode** (`--experiment-config`): Focus on a specific experiment.
   Useful when you want a dedicated worker/evaluator for one experiment.

## Architecture

```
crsbench run --experiment-config exp.yaml
    │
    ├─ 1. register(RuntimeRegistration) → Redis Hash
    ├─ 2. enqueue trial jobs → Redis Queue
    ├─ 3. monitor & collect results
    └─ 4. deregister() → Redis Hash

crsbench worker  (configless — default)
    │
    ├─ connect to Redis
    ├─ list_experiments() → discover queues
    └─ listen on all trial queues

crsbench worker --experiment-config exp.yaml  (config mode)
    │
    ├─ load config → extract queue name
    └─ listen on that experiment's trial queue

crsbench evaluator  (configless — default)
    │
    ├─ connect to Redis
    ├─ list_experiments() → discover queues
    ├─ set up VerificationEngine from registration metadata
    └─ run_multi_queue_supervisor(build_queues, verify_queues)

crsbench evaluator --experiment-config exp.yaml  (config mode)
    │
    ├─ load config → set up engine + pre-builds
    └─ run_ci_supervisor(build_queue, verify_queue)
```

## Redis Structures

| Key | Type | Contents |
|-----|------|----------|
| `crsbench:registry:experiments` | Hash | `experiment_name → RuntimeRegistration JSON` |
| `crsbench:registry:events` | Pub/Sub | `{"event": "register"/"deregister", "experiment": "..."}` |

## Queue Model

- Default queue model is `flat` (`CRSBENCH_QUEUE_MODEL=flat`):
  - trial queue: `crsbench_trial`
  - build queue: `crsbench_build`
  - verify queue: `crsbench_verify`
- Legacy model is `per-experiment` (`CRSBENCH_QUEUE_MODEL=per-experiment`):
  - trial queue: `crsbench_<experiment>`
  - build queue: `crsbench_<experiment>_build`
  - verify queue: `crsbench_<experiment>_verify`
- Flat mode allows a single worker/evaluator pool to serve many experiments.
- Per-experiment mode is still available for strict queue isolation.

## CLI Examples

### Configless worker (default)

```bash
# Discovers experiments from Redis registry automatically
export CRSBENCH_REDIS_HOST=redis-server
crsbench worker --continuous

# With CPU affinity
crsbench worker --continuous --cores 16-47
```

Resource precedence (when cpuset/cgroup supervisor is enabled):
- CLI (`--jobs`, `--cores-per-job`) takes highest priority.
- Otherwise, worker uses experiment metadata from registry (`worker.jobs`, `worker.cores_per_job`).
- Fallback default is `1` job and `4` cores per job.
- Numeric conflicts resolve by `max(...)`.
- For conflicting CPU pinning metadata (`worker.cores`, `worker.skip_cpus`) with no CLI override, selection uses stable experiment-name order.
- Invalid numeric metadata values are rejected at startup (`worker.* >= 1`, `resources.cores_per_trial >= 1`).

### Configless evaluator (default)

```bash
export CRSBENCH_REDIS_HOST=redis-server
crsbench evaluator --build-jobs 8 --build-cores-per-job 2
```

Resource precedence:
- CLI (`--build-jobs`, `--build-cores-per-job`, `--verify-jobs`, `--verify-cores-per-job`, `--cores`, `--skip-cpus`, `--idle-timeout`) takes highest priority.
- Otherwise, evaluator uses experiment metadata from registry (`evaluator.*` block in config).
- Fallback defaults are build jobs `1`, build/verify cores `4`, verify jobs derived from build concurrency, idle timeout `0`.
- Numeric conflicts resolve by `max(...)`.
- For conflicting CPU pinning metadata (`evaluator.cores`, `evaluator.skip_cpus`) with no CLI override, selection uses stable experiment-name order.
- Invalid numeric metadata values are rejected at startup (`evaluator.build_jobs/build_cores_per_job/verify_jobs/verify_cores_per_job >= 1`, `evaluator.idle_timeout >= 0`).

Resource defaults:
- `resources.memory_per_trial` defaults to `null` (unlimited).

CPU tag routing:
- Jobs can carry an optional `cpu_tag` (from `resources.cpu_tag`).
- Worker/evaluator can set `--cpu-tag` (or `worker.cpu_tag` / `evaluator.cpu_tag` in config).
- Untagged workers/evaluators execute only untagged jobs.
- Tagged workers/evaluators execute untagged jobs plus matching `cpu_tag` jobs.
- If configless mode discovers conflicting `cpu_tag` metadata across experiments, startup fails unless CLI `--cpu-tag` is explicitly provided.
- CLI still has highest precedence over metadata values.

### Config mode (single experiment)

```bash
crsbench worker --experiment-config experiment.yaml --continuous
crsbench evaluator --experiment-config experiment.yaml --build-jobs 4
```

### CI mode

```bash
crsbench evaluator --ci --build-jobs 4
```

## Limitations

- **Shared paths**: all experiments on a configless evaluator must share the
  same `benchmarks_root`. If different paths are needed,
  run separate evaluator processes or use `--experiment-config`.

## Implementation

| Module | Role |
|--------|------|
| `crsbench/distributed/registry.py` | `RuntimeRegistration` model + `RegistryClient` |
| `crsbench/run_experiment.py` | Orchestrator registers before enqueue, deregisters after report |
| `crsbench/distributed/worker.py` | `run_worker_configless()` — registry discovery; `main()` — config mode |
| `crsbench/distributed/evaluator.py` | `run_evaluator_configless()` — registry discovery; `run_evaluator_main()` — config mode |
| `crsbench/distributed/ci_supervisor.py` | `run_multi_queue_supervisor()` — multi-experiment queues |
