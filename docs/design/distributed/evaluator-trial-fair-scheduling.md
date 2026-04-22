# Design: Trial-Fair Evaluator Scheduling
- Audience: maintainers working on evaluator dispatch, build/verify queues, and distributed queue semantics
- Scope: scheduler fairness contracts for evaluator-served build and verify work across config-pinned, configless, and CI-compatibility modes
- Related: [Distributed Evaluation](./distributed-evaluation.md), [Distributed Job Queue](./distributed-job-queue.md), [Unified Build & Verify](./unified-build-verify.md), [Configless Runtime](./configless-runtime.md)

## Goals and Non-goals

### Goals
- replace implicit FIFO plus build-first dispatch with an explicit fairness contract
- preserve build-before-verify correctness through dependency gating rather than blanket queue priority
- keep evaluator scheduling correct across restarts, requeues, and non-local execution
- define one fairness model that applies to config-pinned, configless, and CI-compatibility evaluator modes

### Non-goals
- changing worker-side trial queue scheduling
- redefining benchmark-CI DAG semantics or verification verdict semantics
- introducing autoscaling policy or tenant-level quota policy

## Constraints

- verify work may run only after its required build prerequisites are complete or otherwise valid for hydration
- build and verify concurrency limits, CPU-per-job limits, CPU-tag filtering, and dependency release remain authoritative runtime constraints
- queue naming may differ between flat and per-experiment models, but fairness semantics must not depend on queue name ordering
- scheduler state must be recoverable from authoritative Redis/RQ state after evaluator restart or failover
- distributed evaluators may consume the same queue set from different machines

## Context and Boundaries

The evaluator serves build and verify workloads from Redis-backed queues. Today,
dispatch order is implicitly defined by FIFO queue order plus a build-first
queue list. That makes progress depend on submission timing and queue name
position rather than on an explicit policy.

The fairness contract in this document applies only to evaluator-consumed build
and verify work. Trial queues for CRS execution remain governed by their own
worker-side contracts.

## Contract

### Fairness unit

- the primary fairness unit is the logical trial owner key `experiment::trial_id`
- if a queued evaluator job does not belong to an experiment trial, it must use
  a deterministic compatibility owner key derived from the smallest logical work
  unit available in that flow; the scheduler must not fabricate queue-order
  priority for such jobs
- queue family (`build` vs `verify`) and queue name are routing attributes, not
  fairness identities

### Runnable work

- only jobs in the RQ `queued` state are runnable for scheduler fairness
- jobs in `deferred`, `scheduled`, `started`, or terminal states must not
  consume scheduler fairness turns
- dependency-blocked verify jobs are therefore excluded from fairness until
  their prerequisites enqueue them into a runnable queue

### Build gate

- build completion remains a hard prerequisite for dependent verify work
- the scheduler must not use verify fairness to bypass missing build context
- blanket build-first priority over unrelated runnable verify work is not part
  of the contract

### Shared build ownership

- a build job that is shared by multiple blocked trials must be charged to the
  oldest waiting blocked owner key
- later blocked trials may add themselves to the waiting set for that build, but
  they must not displace an older waiting owner while the build remains queued
- once the shared build starts executing, ownership changes are irrelevant for
  that physical attempt

### Ready-order semantics

- fairness must be explicit and scheduler-owned rather than inferred from raw RQ
  FIFO position
- queued evaluator jobs must be admitted into a scheduler-ready structure keyed
  by owner key and queue class (`build` or `verify`)
- within a single owner key and queue class, jobs execute in FIFO admission order
- across owner keys within the same queue class, the scheduler serves queued
  work in round-robin order so one owner cannot monopolize build or verify
  capacity just by enqueueing earlier or more often

### Cross-class dispatch

- build and verify keep separate capacity limits and separate readiness pools
- if both classes have free capacity and runnable work, the scheduler alternates
  class claims instead of treating build as the default winner
- if only one class has free capacity or runnable work, that class may continue
  dispatching without waiting for the other

## Runtime Behavior

### Happy path

1. a worker or submitter enqueues evaluator work with deterministic owner-key metadata
2. build jobs enter build-ready state immediately if they have no unmet dependencies
3. verify jobs enter verify-ready state only when dependency release makes them queued
4. the scheduler claims the next owner key fairly within the eligible class
5. the claimed job is removed from ready state and executed by the evaluator child
6. on build completion, dependent verify jobs become queued and are admitted to fair scheduling

### Retry and requeue behavior

- a pre-start requeue caused by spawn failure, CPU mismatch, or similar
  scheduler-side rejection must preserve the job's owner key and restore it to
  the front of that owner key's class-local ready order
- scheduler-side rejection must not let a retried job jump ahead of older work
  from other owner keys
- if a job is explicitly refreshed or re-enqueued as a new physical attempt, the
  new attempt is admitted as new ready work under the same owner key unless the
  owning flow explicitly changes that identity

### Restart and reconciliation behavior

- the scheduler-ready state must be reconstructable from authoritative queued RQ
  jobs and dependency state
- evaluator startup and configless queue refresh must reconcile queued jobs into
  the ready structure idempotently so fairness does not depend on a clean prior
  shutdown
- stale scheduler metadata must not suppress queued jobs from being dispatched

## Deployment and Distributed Behavior

- fairness semantics must remain correct when submitter, worker, and evaluator
  run on different machines
- when multiple evaluators serve the same queue set, claim and ready-state
  transitions must be atomic at the shared Redis layer so two evaluators cannot
  consume the same fairness turn or the same queued job
- flat and per-experiment queue models must preserve the same fairness contract;
  changing queue naming may change routing topology but must not reintroduce
  queue-order bias

## Decisions and Tradeoffs

- use dependency gating for build-before-verify correctness rather than a hidden
  scheduler bias; this keeps correctness and fairness as separate concerns
- use scheduler-owned ready state because raw FIFO queues cannot express trial
  fairness once multiple owners share the same physical queue
- preserve separate build and verify capacity knobs so operators can tune
  throughput without changing fairness identity
- treat shared builds as oldest-waiter work because build reuse should help
  multiple trials without letting the first enqueuer permanently dominate

## Risks and Validation

- regression risk: verify work could accidentally bypass missing build context
  if dependency admission and scheduler admission diverge
- regression risk: requeue paths could lose owner-key ordering and let one trial
  skip the fairness ring after retries
- distributed risk: restart reconciliation could double-admit or miss queued
  jobs if scheduler-ready state is not rebuilt idempotently

Validation should cover:
- verify jobs never run before required build prerequisites complete
- fair round-robin across trial owners within build-ready work
- fair round-robin across trial owners within verify-ready work
- shared build ownership stays with the oldest blocked owner
- configless multi-queue operation does not regress into queue-name priority
- restart and requeue paths rebuild ready state without losing runnable jobs

## Implementation Pointers

- `crsbench/distributed/ci_supervisor.py`
- evaluator enqueue helpers under `crsbench/distributed/verify_queue.py` and `crsbench/distributed/patch_queue.py`
- evaluator startup/configless admission under `crsbench/distributed/evaluator.py`
- distributed scheduler and evaluator tests under `tests/test_ci_supervisor.py`, `tests/test_evaluator_dual_queue.py`, and related distributed queue tests
