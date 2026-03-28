# TLA+ Model Notes

## Files

- `DistributedTimeoutRecovery.tla`: minimal dual-state model of queue state plus shadow lifecycle state
- `DistributedTimeoutRecovery.cfg`: TLC config that intentionally enables the buggy timeout-requeue path
- `DistributedTimeoutRecoveryHealthy.cfg`: TLC config for the intended protocol under healthy worker/evaluator fairness assumptions
- `DistributedTrialKeySnapshot.tla`: model of physical RQ jobs vs logical `trial_key` snapshots
- `DistributedTrialKeySnapshotDuplicate.cfg`: intentionally allows two physical jobs to share one logical trial key
- `DistributedTrialKeySnapshotUnique.cfg`: fixed configuration where each physical job has a distinct logical trial key
- `DistributedContinueCarryover.tla`: model of continue-mode carryover collection after controller restart
- `DistributedContinueCarryoverBuggy.cfg`: buggy config where continue mode skips re-enqueue and exits without attaching carryover jobs
- `DistributedContinueCarryoverHealthy.cfg`: fixed config where continue mode attaches carryover jobs and writes markers before exit
- `DistributedAttemptOwnership.tla`: model of stale-worker split brain after orphan recovery
- `DistributedAttemptOwnershipBuggy.cfg`: no fencing; superseded worker can still publish
- `DistributedAttemptOwnershipHealthy.cfg`: publication is fenced to the current owner
- `DistributedTerminalMarkers.tla`: model of worker/orchestrator terminal marker publication
- `DistributedTerminalMarkersBuggy.cfg`: buggy config where writers accumulate contradictory markers
- `DistributedTerminalMarkersHealthy.cfg`: fixed config where writers replace the opposite marker
- `DistributedRetryBudget.tla`: model of exact retry-budget behavior during orphan recovery
- `DistributedRetryBudgetBuggy.cfg`: off-by-one retry budget bug that burns the final retry early
- `DistributedRetryBudgetHealthy.cfg`: exact-budget config with one final retry and then permanent failure
- `DistributedRetryMetadataProjection.tla`: model of retry-count projection from lifecycle recovery into RQ metadata
- `DistributedRetryMetadataProjectionBuggy.cfg`: buggy config where lifecycle retry_count increments but RQ metadata stays stale
- `DistributedRetryMetadataProjectionHealthy.cfg`: fixed config where queue-visible retry metadata matches lifecycle retry_count
- `DistributedHeartbeatProjection.tla`: model of heartbeat projection from the side-channel heartbeat hash into lifecycle record fields
- `DistributedHeartbeatProjectionBuggy.cfg`: buggy config where the heartbeat hash is refreshed but lifecycle fields stay stale
- `DistributedHeartbeatProjectionHealthy.cfg`: fixed config where lifecycle heartbeat fields match the side channel
- `DistributedRetryTerminalFence.tla`: model of explicit failed-job retry against terminal lifecycle state
- `DistributedRetryTerminalFenceBuggy.cfg`: buggy config where a failed physical job is retried even though lifecycle already says completed
- `DistributedRetryTerminalFenceHealthy.cfg`: fixed config where explicit retry is fenced to lifecycle records currently in failed
- `DistributedRetryExclusivity.tla`: model of at-most-one authoritative live attempt across retries
- `DistributedRetryExclusivityBuggy.cfg`: buggy config where superseded and replacement attempts are both live
- `DistributedRetryExclusivityHealthy.cfg`: fenced config where only one authoritative live attempt remains
- `DistributedCleanupScope.tla`: model of experiment-scoped cleanup on shared queues
- `DistributedCleanupScopeBuggy.cfg`: buggy config where cleanup drops other experiments' jobs too
- `DistributedCleanupScopeHealthy.cfg`: fixed config where cleanup removes only the targeted experiment
- `DistributedQueueOwnership.tla`: model of queue-monitor ownership rows vs lifecycle ownership
- `DistributedQueueOwnershipBuggy.cfg`: buggy config where queued or failed jobs keep stale worker ownership metadata
- `DistributedQueueOwnershipHealthy.cfg`: fixed config where only running jobs expose an owner
- `DistributedResumeArtifacts.tla`: model of restart reconciliation when terminal artifacts already exist
- `DistributedResumeArtifactsBuggy.cfg`: buggy config where resume ignores artifacts and leaves syncing work unresolved
- `DistributedResumeArtifactsHealthy.cfg`: fixed config where resume collapses artifact-backed work to terminal state
- `DistributedStaleLockResume.tla`: model of continue-mode stale-lock takeover on orchestrator restart
- `DistributedStaleLockResumeBuggy.cfg`: buggy config where continue mode aborts instead of attempting resume
- `DistributedStaleLockResumeHealthy.cfg`: fixed config where stale locks are reclaimed before queue recovery
- `DistributedMonitorCallbacks.tla`: model of orchestrator terminal callback fencing after ownership handoff
- `DistributedMonitorCallbacksBuggy.cfg`: buggy config where stale terminal callbacks consume the job id too early
- `DistributedMonitorCallbacksHealthy.cfg`: fixed config where only the current owner can finalize monitor callbacks
- `DistributedMonitorMarkerWrite.tla`: model of retryable orchestrator marker writes
- `DistributedMonitorMarkerWriteBuggy.cfg`: buggy config where callback failure still consumes the finished job
- `DistributedMonitorMarkerWriteHealthy.cfg`: fixed config where marker-write failure leaves the callback retryable
- `DistributedMonitorLifecycleGate.tla`: model of monitor callbacks after lifecycle has already gone non-active
- `DistributedMonitorLifecycleGateBuggy.cfg`: buggy config where a late callback still writes a marker after lifecycle failed
- `DistributedMonitorLifecycleGateHealthy.cfg`: fixed config where non-active lifecycle records consume callbacks without writing markers
- `DistributedStartedJobRecovery.tla`: model of continue-mode stale started-job recovery
- `DistributedStartedJobRecoveryBuggy.cfg`: buggy config where any live queue worker blocks stale started-job recovery
- `DistributedStartedJobRecoveryHealthy.cfg`: fixed config where stale started jobs recover by their own timeout window
- `DistributedStartedDuplicateRecovery.tla`: model of stale started-duplicate recovery when another runnable peer already exists
- `DistributedStartedDuplicateRecoveryBuggy.cfg`: buggy config where recovery requeues the stale started duplicate and leaves two runnable jobs
- `DistributedStartedDuplicateRecoveryHealthy.cfg`: fixed config where recovery removes the stale duplicate and leaves one active job
- `DistributedCarryoverMarkerStability.tla`: model of restart monitoring when a contradictory carryover terminal duplicate appears after a canonical marker already exists
- `DistributedCarryoverMarkerStabilityBuggy.cfg`: buggy config where the contradictory carryover report overwrites the existing marker
- `DistributedCarryoverMarkerStabilityHealthy.cfg`: fixed config where the preexisting canonical marker remains authoritative
- `DistributedResumeCollection.tla`: model of continue-mode restart when only syncing collection work remains
- `DistributedResumeCollectionBuggy.cfg`: buggy config where continue mode exits before attaching resume collection jobs
- `DistributedResumeCollectionHealthy.cfg`: fixed config where resume-only syncing work is tracked before exit
- `DistributedVisibleTrialResults.tla`: model of final report projection from physical jobs to one logical trial outcome
- `DistributedVisibleTrialResultsBuggy.cfg`: buggy config where the report counts physical terminal jobs directly
- `DistributedVisibleTrialResultsHealthy.cfg`: fixed config where the visible outcome follows the canonical per-trial marker

## Purpose

This first model is meant to catch a specific bug class:

- a job times out
- recovery updates lifecycle state back to `queued`
- but the concrete executable queue entry is not restored

That produces a stalled job even though the lifecycle layer says it is runnable again.

The second model is meant to catch a different code-correspondent bug class:

- trial jobs are physically distinct RQ jobs
- `trial_id` payloads include a run suffix and are not the RQ identity
- queue inspection groups jobs by logical `trial_key`
- duplicate physical jobs for one logical trial collapse to a single visible row

That can hide duplicate work from queue status, cleanup, and orphan recovery.

The third model is meant to catch a restart/continue bug class:

- a previous controller run already left a terminal physical RQ job behind
- the logical trial is skipped in `--queue-mode continue`
- but the replacement controller must still attach that carryover job for
  monitoring so `.success` / `.fail` markers get written

Otherwise the controller can silently drop terminal results during restart.

The fourth model is meant to catch a split-brain bug class:

- worker 1 is marked orphaned and the logical trial is retried
- worker 2 claims the retried logical attempt
- worker 1 was only delayed, not dead, and finishes later

Without ownership fencing, the superseded worker can still publish terminal
artifacts after ownership has moved.

The fifth model is meant to catch contradictory terminal marker bugs:

- a worker or orchestrator writes `.success` / `.fail`
- an opposite terminal marker already exists from an earlier attempt
- the writer must replace the old verdict, not leave both markers behind

The sixth model is meant to catch retry-budget off-by-one bugs:

- a job reaches `retry_count = max_retries - 1`
- stale recovery should grant exactly one final requeue
- the next stale recovery should fail permanently, not early and not late

The seventh model is meant to catch retry exclusivity bugs:

- a logical `trial_key` is retried after stale recovery
- the replacement attempt becomes authoritative
- the protocol must not leave two authoritative live attempts for the same trial

The eighth model is meant to catch cleanup scoping bugs:

- multiple experiments share the same flat queue
- cleanup is requested for one experiment
- only that experiment's jobs may be removed

The ninth model is meant to catch queue-ownership display bugs:

- workers stamp `job.meta["worker_name"]` when they start a job
- retry/requeue can leave that metadata on non-running RQ jobs
- queue-derived operator views must not present that stale metadata as an active owner

The tenth model is meant to catch restart-artifact reconciliation bugs:

- a trial already published `.success` / `.fail`
- lifecycle still says `syncing` when the controller restarts
- resume reconciliation must collapse that record to terminal instead of leaving collection backlog behind

The eleventh model is meant to catch stale-lock restart bugs:

- a prior orchestrator left a stale experiment lock behind
- continue mode should attempt resume rather than abort immediately
- stale-lock takeover must happen before queue recovery mutates existing work

The twelfth model is meant to catch stale terminal-callback bugs:

- lifecycle ownership moves to a replacement worker
- a superseded worker reports `finished` first for the same `job_id`
- the shared queue monitor must not consume that stale terminal event and block
  the current owner's result from writing the orchestrator marker

The thirteenth model is meant to catch marker-write durability bugs:

- a terminal callback runs for a finished job
- the marker write fails transiently
- the callback must not consume the `job_id` until a later retry succeeds

The fourteenth model is meant to catch stale started-job recovery bugs:

- a started job's original owner is gone
- the job has exceeded timeout plus grace
- an unrelated live worker on the same queue must not block requeue of that stale job

The seventeenth model is meant to catch stale started-duplicate recovery bugs:

- a stale `started` duplicate exists for one logical trial
- another physical job for that same trial is already runnable
- continue-mode orphan recovery must not requeue the stale duplicate and create
  two active jobs for the same logical trial

The eighteenth model is meant to catch carryover marker stability bugs:

- continue mode attaches a contradictory terminal duplicate after restart
- the logical trial already has a canonical `.success` / `.fail` marker
- restart monitoring must not let that stale carryover overwrite the existing
  canonical verdict

The nineteenth model is meant to catch worker-side marker stability bugs:

- a physical worker attempt passes the lifecycle ownership fence for its own
  `job_id`
- the logical trial already has a canonical `.success` / `.fail` marker on disk
- a late duplicate physical attempt reports the opposite verdict
- worker-side terminal publication must not overwrite the existing canonical
  verdict

The twentieth model is meant to catch same-session monitor marker stability
bugs:

- no canonical marker exists when monitoring begins
- one physical job writes `.success` for a logical trial
- a later duplicate physical job in the same controller session reports `.fail`
- shared monitor callbacks must preserve the first canonical verdict rather than
  overwrite it

The twenty-first model is meant to catch retry-refresh bring-up bugs:

- continue mode starts with only failed work in its queue snapshot
- failed-job retry requeues that logical trial into active queued work
- cloud bring-up is decided from the controller snapshot
- the snapshot must be refreshed after retry so the controller does not skip
  worker bring-up for newly reactivated work

The twenty-second model is meant to catch explicit retry lifecycle-alignment
bugs:

- continue mode explicitly retries a previously failed physical job
- the shadow lifecycle must be resurrected from `failed` back to `queued`
- active physical attempts and non-terminal lifecycle state must stay aligned

The twenty-third model is meant to catch resume-vs-active convergence bugs:

- a stale syncing attempt is still discoverable through `resume_collection_job_ids`
- an active retry already exists for the same logical trial
- the resume-only candidate must be filtered so it cannot win the canonical
  marker race ahead of the active retry

The twenty-fourth model is meant to catch lifecycle ownership-clearing bugs:

- a worker owns an active attempt
- recovery or terminalization moves the record into a non-active state
- `claimed_by` must be cleared immediately so stale workers cannot keep write
  authority after they have been orphaned, failed, requeued, or completed

The twenty-fifth model is meant to catch non-active lifecycle callback bugs:

- lifecycle has already moved a job into a non-active terminal state
- a late finished callback still arrives from RQ
- the callback may be consumed so monitoring can complete, but it must not write
  a new orchestrator marker once lifecycle is no longer active

The twenty-sixth model is meant to catch retry-metadata projection bugs:

- orphan recovery requeues a concrete RQ job
- lifecycle retry_count increments for the logical job
- queue-derived operator views still read retry_count from RQ metadata
- the concrete metadata must be updated so both layers report the same retry budget state

The twenty-seventh model is meant to catch heartbeat-projection bugs:

- a worker emits a lifecycle heartbeat update
- the separate heartbeat hash is refreshed
- the lifecycle record also exposes `last_heartbeat`
- both layers must advance together so lifecycle snapshots do not lie about liveness

The twenty-eighth model is meant to catch terminal-retry fence bugs:

- continue mode discovers a failed physical RQ job
- the shadow lifecycle for that same job already says `completed`
- explicit retry must not resurrect that stale failed residue behind the
  terminal authoritative record

The fifteenth model is meant to catch resume-collection restart bugs:

- continue mode resumes a stale lock
- lifecycle reconciliation reports job ids still needing collection
- the controller must not exit before attaching that resume-only work to monitoring

The sixteenth model is meant to catch visible-result projection bugs:

- duplicate physical jobs may still exist for one logical trial
- terminal callbacks can therefore produce multiple physical `TrialResult` values
- final reporting must project those values back to one logical trial outcome,
  following the canonical `.success` / `.fail` marker on disk

## Run TLC

TLC is provided by `tla2tools.jar` and requires Java.

This repo can use `.envrc` to expose the jar on `CLASSPATH`:

```bash
source .envrc
java tlc2.TLC -config tla/DistributedTimeoutRecovery.cfg tla/DistributedTimeoutRecovery.tla
```

If you prefer not to rely on `CLASSPATH`:

```bash
java -cp /path/to/tla2tools.jar tlc2.TLC \
  -config tla/DistributedTimeoutRecovery.cfg \
  tla/DistributedTimeoutRecovery.tla
```

Healthy-infra proof run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedTimeoutRecoveryHealthy.cfg \
  tla/DistributedTimeoutRecovery.tla
```

## Expected First Result

With `BuggyRequeueEnabled = TRUE`, the initial model is expected to fail `QueuedMeansExecutable` and produce a short counterexample. That is intentional: the config is set up to demonstrate the timeout-recovery divergence bug class.

Observed counterexample shape:

- `ClaimJob`
- `StartJob`
- `StaleHeartbeat`
- `CrashWorker`
- `TimeoutScanGrace`
- `TimeoutRecoverToQueuedBuggy`

Final bad state:

- `lcState[j] = "queued"`
- `rqState[j] = "rq_absent"`

To explore the intended recovery behavior instead, set:

```tla
BuggyRequeueEnabled = FALSE
```

in `tla/DistributedTimeoutRecovery.cfg` and re-run TLC.

## Stronger Termination Model

`HealthySpec` in `DistributedTimeoutRecovery.tla` adds fairness assumptions for the
case where at least one healthy worker and a healthy evaluator exist:

- queued jobs are eventually claimed
- claimed jobs are eventually started
- running jobs eventually either advance or explicitly fail
- syncing jobs eventually publish artifacts or explicitly fail
- published artifacts are eventually collected into `completed`
- stale heartbeats and timeout recovery are eventually observed

Under those assumptions, `DistributedTimeoutRecoveryHealthy.cfg` checks:

- `NoDuplicateActiveOwner`
- `RetryCountMatchesRequeues`
- `EventuallyCompletedOrFailed`
- `TerminalJobsNeverResurrect`
- `ArtifactTerminalStateMatchesLifecycle`
- `ResumeReconciliationIsComplete`
- `HealthyWorkerCannotBeOrphaned`

That is the stronger guarantee: every job eventually reaches terminal
`completed` or `failed`, and once terminal, it stays terminal.

Verified locally on March 27, 2026:

- `DistributedTimeoutRecovery.cfg` still fails `QueuedMeansExecutable` with the
  intended buggy requeue counterexample
- `DistributedTimeoutRecoveryHealthy.cfg` completes with no TLC errors

## Trial Key Snapshot Model

This model corresponds directly to the distributed trial queue code:

- `run_experiment.py` enqueues trial jobs with a per-run `trial_id` payload
- `get_existing_trials()` in `crsbench/distributed/queue.py` stores one job per
  logical `trial_key`
- `queue_monitor.py` and cleanup/orphan paths used that grouped view

Duplicate-allowed run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedTrialKeySnapshotDuplicate.cfg \
  tla/DistributedTrialKeySnapshot.tla
```

Expected result:

- `NoDuplicateLogicalTrialInFlight` fails
- `SnapshotCoversAllActivePhysicalJobs` fails

Fixed run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedTrialKeySnapshotUnique.cfg \
  tla/DistributedTrialKeySnapshot.tla
```

Expected result:

- TLC completes with no errors

## Attempt Ownership Model

This model corresponds to the stale-worker recovery boundary:

- lifecycle ownership moves via `claimed_by`
- orphan recovery can make a logical trial runnable again
- a replacement worker may start while the old worker is still alive
- publication must be fenced to the current owner, not merely to the job id

Buggy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedAttemptOwnershipBuggy.cfg \
  tla/DistributedAttemptOwnership.tla
```

Expected result:

- `SupersededWorkerCannotPublish` fails

Healthy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedAttemptOwnershipHealthy.cfg \
  tla/DistributedAttemptOwnership.tla
```

Expected result:

- TLC completes with no errors

## Terminal Marker Model

This model corresponds to worker-side and orchestrator-side terminal marker writes:

- `jobs.py` publishes worker-side `.success` / `.fail`
- `run_experiment.py` publishes orchestrator-side `.success` / `.fail`
- a fresh write must replace the opposite terminal marker rather than accumulate both

Buggy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedTerminalMarkersBuggy.cfg \
  tla/DistributedTerminalMarkers.tla
```

Expected result:

- `NoTerminalMarkerContradiction` fails

Healthy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedTerminalMarkersHealthy.cfg \
  tla/DistributedTerminalMarkers.tla
```

Expected result:

- TLC completes with no errors

## Retry Budget Model

This model corresponds to retry counting during orphan recovery:

- `retry_count` is stored in the lifecycle shadow record
- orphan recovery either requeues and increments, or permanently fails
- the budget boundary must allow the final retry exactly once

Buggy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedRetryBudgetBuggy.cfg \
  tla/DistributedRetryBudget.tla
```

Expected result:

- `NoEarlyPermanentFailure` fails

Healthy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedRetryBudgetHealthy.cfg \
  tla/DistributedRetryBudget.tla
```

Expected result:

- TLC completes with no errors

## Retry Exclusivity Model

This model corresponds to one logical trial being retried after a stale-worker recovery:

- the original attempt starts first
- stale recovery makes a replacement attempt authoritative
- only one authoritative live attempt may remain for that `trial_key`

Buggy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedRetryExclusivityBuggy.cfg \
  tla/DistributedRetryExclusivity.tla
```

Expected result:

- `AtMostOneLiveAttemptPerTrialKeyAcrossRetries` fails

Healthy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedRetryExclusivityHealthy.cfg \
  tla/DistributedRetryExclusivity.tla
```

Expected result:

- TLC completes with no errors

## Cleanup Scope Model

This model corresponds to experiment-scoped cleanup on shared queues:

- multiple experiments share one queue
- cleanup for one experiment should remove only that experiment's jobs
- unrelated jobs must survive the cleanup pass

Buggy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedCleanupScopeBuggy.cfg \
  tla/DistributedCleanupScope.tla
```

Expected result:

- `CleanupScopedToExperiment` fails

Healthy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedCleanupScopeHealthy.cfg \
  tla/DistributedCleanupScope.tla
```

Expected result:

- TLC completes with no errors

## Queue Ownership Model

This model corresponds to queue-derived ownership rows:

- workers stamp `job.meta["worker_name"]` when they start running work
- RQ metadata can outlive active ownership after retry, requeue, or failure
- queue monitor views must only expose `claimed_by` for concrete running jobs

Buggy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedQueueOwnershipBuggy.cfg \
  tla/DistributedQueueOwnership.tla
```

Expected result:

- `LifecycleMatchesRQOwnership` fails

Healthy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedQueueOwnershipHealthy.cfg \
  tla/DistributedQueueOwnership.tla
```

Expected result:

- TLC completes with no errors

## Resume Artifact Model

This model corresponds to restart reconciliation when terminal artifacts already
exist:

- a worker already published `.success` / `.fail`
- lifecycle is still `syncing` when the controller restarts
- resume reconciliation must collapse that record to terminal instead of
  returning collection backlog for already-terminal work

Buggy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedResumeArtifactsBuggy.cfg \
  tla/DistributedResumeArtifacts.tla
```

Expected result:

- `NoArtifactBackedSyncingAfterResume` fails

Healthy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedResumeArtifactsHealthy.cfg \
  tla/DistributedResumeArtifacts.tla
```

Expected result:

- TLC completes with no errors

## Stale Lock Resume Model

This model corresponds to continue-mode lock handoff after an orchestrator restart:

- continue mode first tries normal lock acquisition
- if the previous controller left a stale lock behind, resume should reclaim it
- queue recovery should proceed only after the stale lock has been reclaimed

Buggy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedStaleLockResumeBuggy.cfg \
  tla/DistributedStaleLockResume.tla
```

Expected result:

- `StaleLockCanRecover` fails

Healthy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedStaleLockResumeHealthy.cfg \
  tla/DistributedStaleLockResume.tla
```

Expected result:

- TLC completes with no errors

## Continue Carryover Model

This model corresponds directly to the continue-mode restart path:

- `run_experiment.py` skips enqueue when `get_existing_trials()` reports an
  existing logical trial key
- terminal carryover jobs still need to pass through `monitor_jobs()` so the
  orchestrator marker callbacks run
- exiting before attaching those jobs loses terminal results

Buggy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedContinueCarryoverBuggy.cfg \
  tla/DistributedContinueCarryover.tla
```

Expected result:

- `DoneImpliesMarker` fails because the controller exits with no marker written

Fixed run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedContinueCarryoverHealthy.cfg \
  tla/DistributedContinueCarryover.tla
```

Expected result:

- TLC completes with no errors

## Monitor Callback Model

This model corresponds to the shared queue monitor callback boundary:

- `_process_tracked_jobs()` keeps `seen_finished` per `job_id`
- `_build_monitor_callbacks()` writes orchestrator markers from finished jobs
- after lifecycle ownership moves, a stale worker's terminal event must not
  consume the `job_id` before the current owner's result arrives

Buggy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedMonitorCallbacksBuggy.cfg \
  tla/DistributedMonitorCallbacks.tla
```

Expected result:

- `StaleOwnerCannotConsumeFinishedCallback` fails

Healthy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedMonitorCallbacksHealthy.cfg \
  tla/DistributedMonitorCallbacks.tla
```

Expected result:

- TLC completes with no errors

## Monitor Marker Write Model

This model corresponds to the marker-write retry boundary:

- `_build_monitor_callbacks()` writes orchestrator terminal markers
- `_process_tracked_jobs()` only advances once the callback accepts the event
- a transient marker-write failure must leave the finished job retryable

Buggy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedMonitorMarkerWriteBuggy.cfg \
  tla/DistributedMonitorMarkerWrite.tla
```

Expected result:

- `WriteFailureCannotConsumeFinishedJob` fails

Healthy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedMonitorMarkerWriteHealthy.cfg \
  tla/DistributedMonitorMarkerWrite.tla
```

Expected result:

- TLC completes with no errors

## Started Job Recovery Model

This model corresponds to continue-mode queue recovery for started jobs:

- `handle_orphaned_jobs()` examines started trial jobs on restart
- stale started jobs should be requeued by their own timeout-plus-grace window
- a different live worker on the same queue must not suppress that recovery

Buggy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedStartedJobRecoveryBuggy.cfg \
  tla/DistributedStartedJobRecovery.tla
```

Expected result:

- `UnrelatedWorkerCannotBlockStaleRecovery` fails

Healthy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedStartedJobRecoveryHealthy.cfg \
  tla/DistributedStartedJobRecovery.tla
```

Expected result:

- TLC completes with no errors

## Resume Collection Model

This model corresponds to continue-mode restart when only syncing collection
work remains:

- `resume_or_raise()` can report job ids still needing collection
- there may be no visible queue entries and no new trials
- continue mode must still attach those resumed jobs before any early exit

Buggy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedResumeCollectionBuggy.cfg \
  tla/DistributedResumeCollection.tla
```

Expected result:

- `NeedsCollectionPreventsEarlyExit` fails

Healthy run:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedResumeCollectionHealthy.cfg \
  tla/DistributedResumeCollection.tla
```

Expected result:

- TLC completes with no errors
