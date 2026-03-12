# Configless Worker/Evaluator Runtime

Audience: contributors changing worker/evaluator registration, queue discovery, or configless runtime behavior.
Scope: configless runtime registration, queue discovery, and resource-routing contracts.

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

Runtime registration and queue discovery work as follows:

- the orchestrator registers experiment metadata in Redis before enqueueing jobs
- configless workers discover trial queues from the registry and may serve multiple experiments
- configless evaluators discover build/verify queues from the registry and may serve multiple experiments
- config-pinned workers and evaluators use one explicit experiment configuration and one queue set

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

## Evaluator Startup Semantics

Configless evaluators use the same build and verify queues as config-pinned
evaluators, but startup behavior differs:

- config-pinned evaluator CLI mode normally enqueues startup pre-build jobs
  before entering steady-state supervision
- configless evaluator mode does not enqueue startup pre-build jobs
- configless evaluators still consume build queues lazily, and verify work may
  trigger on-demand builds on cache miss

So configless mode keeps build/verify queue separation, but it does not have
the same normal startup build-first phase as config-pinned evaluator CLI mode.

## Resource and Routing Contracts

Resource precedence (when cpuset/cgroup supervisor is enabled):
- CLI (`--jobs`, `--cores-per-job`) takes highest priority.
- Otherwise, worker uses experiment metadata from registry (`worker.jobs`, `worker.cores_per_job`).
- Fallback default is `1` job and `4` cores per job.
- Numeric conflicts resolve by `max(...)`.
- CPU pinning is CLI-owned in configless mode (`--cpuset`, `--skip-cpuset`).
- Without `--cpuset`/`--skip-cpuset`, CPU affinity is disabled.
- Invalid numeric metadata values are rejected at startup (`worker.* >= 1`, `resources.cores_per_trial >= 1`).

Resource precedence:
- CLI (`--build-jobs`, `--build-cores-per-job`, `--verify-jobs`, `--verify-cores-per-job`, `--cpuset`, `--skip-cpuset`, `--idle-timeout`) takes highest priority.
- Otherwise, evaluator uses experiment metadata from registry (`evaluator.*` block in config).
- Fallback defaults are build jobs `1`, build/verify cores `4`, verify jobs derived from build concurrency, idle timeout `0`.
- Numeric conflicts resolve by `max(...)`.
- CPU pinning is CLI-owned in configless mode (`--cpuset`, `--skip-cpuset`).
- Without `--cpuset`/`--skip-cpuset`, CPU affinity is disabled.
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

Operator-facing command usage for configless mode, config-pinned mode, and
legacy CI queue compatibility is documented in:

- [Distributed Experiments](../../guides/experiments/distributed.md)
- [Benchmark CI Distributed](../../guides/benchmark-ci/distributed.md)

## Limitations

- **Shared paths**: all experiments on a configless evaluator must share the
  same `benchmarks_root`. If different paths are needed,
  run separate evaluator processes or use `--experiment-config`.
- **Shared inc-image policy**: all experiments on a configless evaluator must
  have compatible inc-image settings (`inc_image_policy`, `inc_image_registry`,
  pull-timeout/size limits, and image prefix). These settings affect local
  image reuse, remote pull attempts, remote size gating, and local build
  fallback. If these differ, split evaluators.

## Implementation Pointers

| Module | Role |
|--------|------|
| `crsbench/distributed/registry.py` | `RuntimeRegistration` model + `RegistryClient` |
| `crsbench/run_experiment.py` | Orchestrator-side registration lifecycle |
| `crsbench/distributed/worker.py` | Worker registry discovery and config-pinned mode entrypoints |
| `crsbench/distributed/evaluator.py` | Evaluator registry discovery and config-pinned mode entrypoints |
| `crsbench/distributed/ci_supervisor.py` | Multi-queue supervisor coordination |
