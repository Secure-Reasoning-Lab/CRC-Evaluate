# Design: Distributed Job Queue
- Audience: maintainers working on distributed orchestration and queue-backed execution
- Scope: queue semantics for `crsbench run`, `worker`, and related job execution paths
- Related: [Distributed Evaluation](./distributed-evaluation.md), [Deployment Guide](./deployment-guide.md), [Configless Runtime](./configless-runtime.md)

## Goals and Non-goals

### Goals
- define the queue-backed execution contract for distributed runs
- preserve reliable job tracking across orchestrator, workers, and evaluator processes
- make retry, stale-job, and non-local execution semantics explicit

### Non-goals
- operator command walkthroughs
- code-level implementation snapshots
- autoscaling or multi-tenant scheduling policy

## Context and Boundaries

The distributed runtime uses Valkey/Redis-compatible queues to decouple:
- orchestration and trial expansion
- trial execution on workers
- build/verify execution on evaluators

The queue layer is the contract boundary between these roles. Callers submit concrete jobs; workers and evaluators consume them without assuming a shared process or shared local state.

## Queue Contract

### Core properties
- job identity must be deterministic for the same logical unit of work
- queue payloads must be self-describing enough for non-local execution
- consumers must tolerate Redis-backed state that outlives a single process lifetime
- distributed operation must not rely on in-memory state held by the submitting process

### Queue families
- trial queues carry CRS trial execution jobs
- build queues carry variant-build and preparation jobs
- verify queues carry POV, patch, and related verification jobs

Queue naming and discovery may differ between config-pinned and configless modes, but the execution semantics are the same.

## Job Classes

### Trial jobs
Trial jobs execute one fully-expanded experiment unit:
- CRS service
- benchmark + harness
- mode
- sanitizer
- trial number
- optional CPV target

The RQ `job_id` for a trial job is deterministic for that logical trial. The
runtime payload `trial_id` may still include a per-run suffix for filesystem or
compose isolation, but that payload field is not the queue identity.

### Build jobs
Build jobs prepare artifacts needed by later verification or CI stages. Build jobs are authoritative for build-context creation; verify paths must not silently rebuild missing prerequisites unless the owning flow explicitly allows that.

### Verify jobs
Verify jobs consume prepared artifacts and produce verdicts. In non-local deployments, verify payloads must not assume the producer and consumer share a filesystem.

## Identity and Deduplication

- every logical job type must have a deterministic `job_id`
- duplicate enqueue attempts must resolve to reuse or explicit refresh, not ambiguous duplication
- build and verify jobs must encode the mode/sanitizer/source parameters that affect artifact compatibility
- stale queued/deferred/scheduled records may be refreshed when the orchestrator intentionally re-enqueues work
- operator status, cleanup, and orphan recovery must reason over physical queue
  jobs; a grouped `trial_key -> job` snapshot is only a logical summary view and
  must not hide duplicate physical jobs from maintenance paths

## State and Failure Semantics

### Shadow lifecycle contract
Cloud-backed trial execution maintains a Redis-backed shadow lifecycle in addition
to the concrete RQ job state. The shadow lifecycle exists to make stale-worker
recovery explicit across controller restarts and cloud-instance loss.

The active lifecycle states are:
- `queued`
- `claimed`
- `running`
- `syncing`

The terminal lifecycle states are:
- `completed`
- `failed`

`orphaned` is an internal recovery state entered only while stale-job handling is
in progress.

### Terminal states
The queue layer treats the following as terminal outcomes:
- `finished`
- `failed`
- `stopped`
- `canceled`
- worker-side and orchestrator-side terminal marker writers must leave exactly
  one verdict marker on disk for a trial; when publishing a new canonical
  verdict, writing `.success` must remove stale `.fail`, and writing `.fail`
  must remove stale `.success`
- orchestrator-visible final reporting must collapse duplicate physical attempt
  results onto one logical trial outcome; the canonical verdict is the terminal
  marker state in the logical trial directory, not the count of physical RQ jobs

### Stale or missing state
- missing queue metadata for a supposedly pending job is an infrastructure failure
- `started` jobs that exceed timeout plus grace are treated as stale infrastructure failures
- continue-mode recovery of `started` jobs must use per-job timeout-plus-grace
  staleness; the presence of another live worker on the same queue must not
  suppress recovery of a stale started job
- if a stale `started` duplicate exists while another physical job for the same
  logical trial is already runnable, recovery must remove the stale duplicate
  instead of requeueing it and creating two active jobs
- timeout recovery must not leave the shadow lifecycle in `queued` unless the
  concrete queue entry is executable again
- once `claimed_by` moves to a replacement worker, the superseded worker must
  stop emitting shadow-lifecycle heartbeats and must not publish terminal
  worker-side artifacts for that logical trial
- worker-side terminal publication must also preserve a preexisting
  contradictory canonical marker for the logical trial; a late duplicate
  physical attempt must not flip `.success` to `.fail` or vice versa
- orchestrator-side queue monitoring must apply the same ownership fence before
  consuming finished/failed callbacks; a stale terminal event must not mark the
  `job_id` as processed ahead of the current owner
- orchestrator-side terminal callbacks must only mark a `job_id` as processed
  after the corresponding `.success` / `.fail` marker write succeeds; transient
  marker-write failures must remain retryable
- queue-derived operator views must only expose an active owner for concrete
  running jobs; queued/deferred/terminal RQ entries may retain stale
  `worker_name` metadata and must not be treated as currently claimed
- published terminal artifacts on orchestrator storage are authoritative for
  stale-job recovery: `.success` maps to `completed`, `.fail` maps to `failed`
- retry policy must distinguish infra failures from benchmark-result failures

### Retry principles
- retries must preserve deterministic job identity semantics
- retry budget is exact: a job at `max_retries - 1` gets one final requeue, and
  the next stale recovery must fail permanently rather than requeue again
- active non-terminal jobs are reused rather than duplicated
- refresh of terminal jobs is a policy decision at submit time, not a hidden worker behavior

## Non-local Execution Constraints

- worker and evaluator nodes may not share a filesystem
- job payloads must therefore include enough information to rehydrate the required context remotely
- local cache hydration is allowed only when it preserves the authoritative build/verify boundary
- queue-driven execution must remain correct when jobs are consumed on different machines from the submitter

## Runtime Behavior

### Worker stdin contract
- distributed worker execution must not depend on an interactive stdin
- worker entry paths must detach stdin before entering RQ worker or supervisor execution
- this prevents background or service-managed worker processes from receiving `SIGTTIN` when forked work-horses or their subprocesses attempt terminal reads
- this requirement applies across burst, continuous, single-worker, and multi-worker execution paths

### Happy path
1. orchestrator expands experiment work into concrete jobs
2. jobs are enqueued with deterministic identities
3. workers/evaluators dequeue jobs according to role-specific capacity
4. results are persisted back through queue/job state
5. orchestrator or downstream consumers aggregate terminal outcomes

### Failure path
1. dequeue or execution fails
2. failure is recorded with infrastructure-vs-result semantics
3. stale-worker recovery first checks for published terminal artifacts, then
   either marks the shadow lifecycle terminal or re-enqueues the concrete job
4. retry/refresh policy decides whether the logical job is resubmitted or treated as terminal
5. aggregate reporting preserves `ERROR` vs `FAIL` distinctions where applicable

### Continue-mode restart behavior
- `crsbench run --queue-mode continue` may skip enqueue for a logical trial when
  an existing queue record already represents that trial
- if a prior orchestrator left a stale experiment lock behind, continue mode
  must attempt stale-lock takeover before aborting queue recovery
- if the existing physical job is already terminal but the orchestrator marker is
  still missing, the controller must attach that carryover job to the monitor and
  materialize `.success` / `.fail` before exit
- if a canonical orchestrator marker already exists for a logical trial, a
  contradictory carryover terminal duplicate must not overwrite that marker
  during restart monitoring
- if stale-lock resume reports job ids still needing collection, continue mode
  must attach those jobs even when there are no visible queue entries and no new
  trials to enqueue; resume-only syncing work must not trigger early exit
- restart reconciliation must check published terminal artifacts before leaving a
  lifecycle record in `syncing`; artifact-backed in-flight records should be
  collapsed to `completed` / `failed` rather than left waiting for collection
- continue mode must not silently drop terminal carryover jobs just because no
  new trial was enqueued for the same logical trial

## Decisions and Tradeoffs

- decision: use queue-backed role separation rather than a monolithic runtime
  - tradeoff: higher operational complexity for better failure isolation and scaling
- decision: deterministic job identity
  - tradeoff: stricter parameter modeling in exchange for safer dedup/retry behavior
- decision: explicit stale-job handling
  - tradeoff: more queue bookkeeping in exchange for reliable recovery from orphaned workers

## Validation

This contract should be covered by:
- distributed worker/job-state tests
- queue deduplication and refresh-policy tests
- non-local build/verify hydration tests
- docs contract checks that keep this page contract-level rather than implementation-level

## Implementation Pointers

- `crsbench/distributed/jobs.py`
- `crsbench/distributed/worker.py`
- `crsbench/distributed/evaluator.py`
- `crsbench/distributed/queue.py`
