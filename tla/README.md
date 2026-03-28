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
