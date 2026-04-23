# Design: Trial-Fair Evaluator Scheduling
- Audience: maintainers working on evaluator dispatch, build/verify queues, and distributed queue semantics
- Scope: scheduler fairness contracts for evaluator-served build and verify work across config-pinned, configless, and CI-compatibility modes
- Related: [Distributed Evaluation](./distributed-evaluation.md), [Distributed Job Queue](./distributed-job-queue.md), [Unified Build & Verify](./unified-build-verify.md), [Configless Runtime](./configless-runtime.md), [TLA+ Fairness Model](./EvaluatorTrialFairScheduling.tla)

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

## Formal Model

- `EvaluatorTrialFairScheduling.tla` and
  `EvaluatorTrialFairScheduling.cfg` provide a bounded TLA+ model for this
  fairness revision
- the model checks abstract partition safety so claim failures and local
  pre-start retry paths do not silently lose queued jobs
- the model checks build-before-verify gating for verify work
- the model checks owner no-starvation for a canonical two-owner scenario with
  bounded transient claim failures
- the model abstracts the intermediate claim handoff into recoverable pre-start
  failure and restore transitions rather than modeling Redis list internals
- the model does not represent `cpu_tag` filtering, non-continuous
  cpu-tag-livelock exit behavior, or Redis lease TTL/refresh timing for
  in-flight claims; those remain runtime behaviors validated by Python tests
- the model deliberately does not specify a distributed global owner-turn
  ledger, because this revision only implements local fair selection over
  shared queued state plus atomic Redis job claims

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

- a queued shared build carries exactly one scheduler owner key in job metadata
- if duplicate reuse encounters a queued shared build whose owner key is still a
  generic compatibility identity, the first trial-scoped reuse may adopt that
  queued job and replace the generic owner key
- once a queued shared build already carries a trial owner key, later reuses do
  not displace that owner in this revision
- once the shared build starts executing, ownership changes are irrelevant for
  that physical attempt

### Ready-order semantics

- fairness must be explicit and scheduler-owned rather than inferred from raw RQ
  FIFO position
- on each selection attempt, the evaluator derives a scheduler-ready view from
  currently queued RQ jobs, keyed by owner key and queue class (`build` or
  `verify`)
- this revision does not persist a separate ready ledger beyond queued RQ jobs,
  queue membership, and job metadata
- within a single owner key and queue class, jobs execute in FIFO order as
  observed from the current queued RQ list, except explicit scheduler-side
  requeues that restore a job to the queue front for retry
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

- a claim or pre-start rejection must preserve the job's owner key and must not
  silently lose runnable work; before execution start is committed, the job must
  remain either in its runnable queue or in a recoverable intermediate claim
  state
- the runtime currently uses a Redis-backed fair-claim lease to distinguish an
  in-progress pre-start handoff from an abandoned claim; lease refresh and
  expiry are implementation details, not part of the TLA+ abstraction
- retries caused by spawn failure, CPU allocation failure, or similar local
  retry paths restore the queued job to the queue front so the same attempt can
  be retried without reserialization
- worker capability filtering such as `cpu_tag` matching is applied before fair
  selection when possible; any post-claim fallback requeue still preserves the
  job metadata and returns the job to queued state
- if a job is explicitly refreshed or re-enqueued as a new physical attempt, the
  new attempt is admitted as new ready work under the same owner key unless the
  owning flow explicitly changes that identity

### Restart and reconciliation behavior

- the scheduler-ready view must be reconstructable from authoritative queued RQ
  jobs, queue membership, and dependency state
- stale intermediate claim state from an evaluator crash or startup failure must
  be reconciled back into runnable or terminal state without silently
  discarding the job
- evaluator startup and configless queue refresh recompute that derived ready
  view idempotently so fairness does not depend on a clean prior shutdown
- stale scheduler metadata must not suppress queued jobs from being dispatched

## Deployment and Distributed Behavior

- fairness semantics must remain correct when submitter, worker, and evaluator
  run on different machines
- when multiple evaluators serve the same queue set, job-claim transitions must
  be atomic at the shared Redis layer so two evaluators cannot consume the same
  queued job or leave it untracked between queue and execution-start state
- each evaluator applies the same local fair-selection algorithm over the shared
  queued state; this revision does not add a separate distributed turn ledger
  for globally serialized owner rotation across evaluators
- flat and per-experiment queue models must preserve the same fairness contract;
  changing queue naming may change routing topology but must not reintroduce
  queue-order bias

## Decisions and Tradeoffs

- use dependency gating for build-before-verify correctness rather than a hidden
  scheduler bias; this keeps correctness and fairness as separate concerns
- use a scheduler-owned ready view derived from queued jobs because raw FIFO
  queues cannot express trial fairness once multiple owners share the same
  physical queue
- preserve separate build and verify capacity knobs so operators can tune
  throughput without changing fairness identity
- prefer explicit queued owner metadata over queue-name ordering; build reuse
  can upgrade a generic shared build to a trial owner without needing a separate
  waiting-set data structure

## Risks and Validation

- regression risk: verify work could accidentally bypass missing build context
  if dependency admission and scheduler admission diverge
- regression risk: claim/requeue paths could silently drop a queued job or skew
  local fair-turn state after transient failures
- distributed risk: restart reconciliation could miss queued jobs if the
  derived ready view is not rebuilt idempotently

Validation should cover:
- verify jobs never run before required build prerequisites complete
- fair round-robin across trial owners within build-ready work
- fair round-robin across trial owners within verify-ready work
- duplicate reuse can upgrade a generic shared build owner to a trial owner
- configless multi-queue operation does not regress into queue-name priority
- restart and requeue paths rebuild the ready view without losing runnable jobs
- runtime tests cover cpu-tag filtering, livelock exit, and fair-claim lease
  recovery edges
- the bounded TLA+ model preserves abstract no-silent-loss safety and owner
  no-starvation under transient claim failures

## Implementation Pointers

- `crsbench/distributed/ci_supervisor.py`
- evaluator enqueue helpers under `crsbench/distributed/verify_queue.py` and `crsbench/distributed/patch_queue.py`
- evaluator startup/configless admission under `crsbench/distributed/evaluator.py`
- distributed scheduler and evaluator tests under `tests/test_ci_supervisor.py`, `tests/test_evaluator_dual_queue.py`, and related distributed queue tests
