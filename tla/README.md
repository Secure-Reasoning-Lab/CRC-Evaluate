# TLA+ Models

This directory holds small, code-correspondent TLA+ models for the distributed
queue, worker, and orchestrator paths in CRSBench.

The models are not a full system model. Each one isolates a concrete boundary
where the Python runtime previously had a bug or where state can drift across:

- RQ queue state
- shadow lifecycle state in Redis
- marker files on disk
- controller restart and resume state
- queue-derived operator views

## How To Read This Directory

Most models come as a triplet:

- `Model.tla`: the abstract state machine
- `ModelBuggy.cfg`: enables the historical or intentionally unsafe behavior
- `ModelHealthy.cfg`: enables the intended behavior and should pass TLC

Some boundaries have more than one materially distinct unsafe mode. Those
models may carry multiple buggy configs so each counterexample stays focused.

Two older models use a different naming pattern:

- `DistributedTimeoutRecovery.cfg` / `DistributedTimeoutRecoveryHealthy.cfg`
- `DistributedTrialKeySnapshotDuplicate.cfg` / `DistributedTrialKeySnapshotUnique.cfg`

## Running TLC

### Install TLC

TLC runs from `tla2tools.jar` and requires Java.

Stable releases are published here:

- https://github.com/tlaplus/tlaplus/releases

Typical install:

```bash
mkdir -p "$HOME/tla"
cd "$HOME/tla"
curl -L -O https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar
java -cp "$HOME/tla/tla2tools.jar" tlc2.TLC -help
```

If you want the jar on `CLASSPATH` instead:

```bash
export CLASSPATH="$HOME/tla/tla2tools.jar"
java tlc2.TLC -help
```

If `.envrc` points `CLASSPATH` at `tla2tools.jar`, run TLC like this:

```bash
source .envrc
java tlc2.TLC -config tla/DistributedTimeoutRecovery.cfg tla/DistributedTimeoutRecovery.tla
```

Without `CLASSPATH`:

```bash
java -cp /path/to/tla2tools.jar tlc2.TLC \
  -config tla/DistributedTimeoutRecovery.cfg \
  tla/DistributedTimeoutRecovery.tla
```

Recommended first checks:

```bash
source .envrc
java tlc2.TLC \
  -config tla/DistributedTimeoutRecovery.cfg \
  tla/DistributedTimeoutRecovery.tla

java tlc2.TLC \
  -config tla/DistributedTimeoutRecoveryHealthy.cfg \
  tla/DistributedTimeoutRecovery.tla
```

The buggy config should fail with the intended counterexample. The healthy
config should complete with no TLC errors.

## Model Catalog

### Recovery And Lifecycle

| Model | Runtime boundary | Buggy config catches | Healthy config guarantees |
| --- | --- | --- | --- |
| `DistributedTimeoutRecovery` | timeout recovery after stale worker loss | lifecycle returns to `queued` without restoring executable queue state | timeout recovery keeps queue and lifecycle aligned |
| `DistributedRetryBudget` | orphan recovery retry budget | final retry is burned too early or too late | retry budget is exact |
| `DistributedRetryMetadataProjection` | retry count projected from lifecycle into queue metadata | lifecycle retry count changes but queue-visible metadata stays stale | operator-visible retry metadata matches lifecycle |
| `DistributedHeartbeatProjection` | heartbeat side channel projected into lifecycle rows | heartbeat hash advances but lifecycle `last_heartbeat` stays stale | lifecycle snapshots reflect current heartbeat state |
| `DistributedRetryLifecycleAlignment` | explicit retry rollback around failed requeue | lifecycle resurrects to `queued` and stays there after enqueue failure | lifecycle rolls back to `failed` when requeue fails |
| `DistributedRetryTerminalFence` | explicit retry of stale failed RQ residue | retry resurrects work behind a lifecycle row already marked terminal | explicit retry is fenced to lifecycle rows still in `failed` |
| `DistributedLifecycleOwnership` | clearing `claimed_by` on non-active transitions | stale worker retains ownership after orphan, fail, queue, or completion | non-active lifecycle states clear ownership immediately |
| `DistributedInitHeartbeatFence` | worker startup heartbeat | startup heartbeat refreshes terminal or foreign-owned rows | startup heartbeat requires active ownership |
| `DistributedStartupOwnershipFence` | worker startup `claimed -> running` transition | startup steals another worker's claim | startup only advances jobs already owned by the current worker |

### Restart, Resume, And Continue Mode

| Model | Runtime boundary | Buggy config catches | Healthy config guarantees |
| --- | --- | --- | --- |
| `DistributedContinueCarryover` | continue-mode carryover monitoring | controller skips enqueue and exits without attaching carryover terminal jobs | carryover jobs are attached so markers are written before exit |
| `DistributedResumeArtifacts` | restart reconciliation with existing terminal artifacts | restart leaves artifact-backed work in `syncing` | resume collapses artifact-backed work to terminal |
| `DistributedStaleLockResume` | stale experiment lock takeover | continue mode aborts instead of reclaiming a stale lock | stale locks are reclaimed before recovery proceeds |
| `DistributedResumeCollection` | restart with resume-only syncing backlog | controller exits before attaching collection-only work | resume-only collection work is attached before exit |
| `DistributedResumeRegistryOwnership` | resumed controller ownership of experiment registry state | resumed controller never adopts registry state strongly enough for cleanup | resumed controller owns registry state and cleanup can clear it |
| `DistributedResumeActiveConvergence` | overlap between resume-only syncing work and an active retry | stale resume-only candidate wins the canonical marker race | resume-only candidate is filtered when active retry already exists |
| `DistributedRetryRefreshBringup` | continue-mode retry followed by cloud bring-up | controller decides bring-up from stale snapshot and misses newly requeued work | snapshot is refreshed before bring-up decisions |
| `DistributedStartedJobRecovery` | continue-mode stale started-job recovery | unrelated live worker blocks stale started-job recovery | stale started jobs recover by their own timeout window |
| `DistributedStartedDuplicateRecovery` | continue-mode recovery with stale started duplicates | stale duplicate is requeued even though another runnable peer already exists | stale duplicate is removed and only one active job remains |

### Evaluator Build And Verify Coordination

| Model | Runtime boundary | Buggy config catches | Healthy config guarantees |
| --- | --- | --- | --- |
| `DistributedAsyncPovVerifyDrain` | async POV verification build prerequisites and final drain budget | verify starts before its build DAG resolves, or drain times out on a shortened budget | verify consumes only explicit prebuilt variants and drain spends the full `verify_timeout` budget |
| `EvaluatorTrialFairScheduling` | evaluator trial-fair build/verify dispatch | transient claim failures or queue-order bias silently break fairness or lose pre-start work | owner-aware fair selection preserves build-gated verify ordering and no-silent-loss pre-start recovery |

### Ownership, Callbacks, And Marker Writes

| Model | Runtime boundary | Buggy config catches | Healthy config guarantees |
| --- | --- | --- | --- |
| `DistributedAttemptOwnership` | stale-worker split brain after retry/orphan recovery | superseded worker still publishes after ownership moved | publication is fenced to the current owner |
| `DistributedMonitorCallbacks` | shared queue finished-callback handling | stale callback consumes the `job_id` before the current owner's result arrives | stale callback cannot consume the authoritative finished event |
| `DistributedMonitorMarkerWrite` | retryable orchestrator marker writes | transient marker-write failure still consumes finished job state | marker-write failure leaves callback retryable |
| `DistributedMonitorLifecycleGate` | late finished callback after lifecycle is already non-active | callback writes a new orchestrator marker after lifecycle already failed or completed | callback may be consumed but cannot write a new marker once lifecycle is non-active |
| `DistributedTerminalMarkers` | worker-side and orchestrator-side terminal marker publication | `.success` and `.fail` accumulate together | opposite terminal marker is replaced |
| `DistributedWorkerMarkerStability` | worker-side publication after canonical marker exists | late duplicate worker overwrites canonical verdict | existing canonical marker stays authoritative |
| `DistributedSessionMarkerStability` | same-session monitor callbacks for duplicate physical jobs | later duplicate callback flips earlier canonical marker | first canonical session verdict stays authoritative |
| `DistributedCarryoverMarkerStability` | continue-mode monitoring of contradictory carryover duplicates | stale carryover report overwrites existing canonical marker | preexisting canonical marker remains authoritative |

### Queue Projection, Reporting, And Cleanup

| Model | Runtime boundary | Buggy config catches | Healthy config guarantees |
| --- | --- | --- | --- |
| `DistributedTrialKeySnapshot` | physical RQ jobs projected into logical `trial_key` rows | duplicate physical jobs collapse to one visible logical row | snapshot covers all active physical jobs |
| `DistributedRetryExclusivity` | retry after stale-worker recovery | two authoritative live attempts remain for one logical trial | at most one authoritative live attempt remains |
| `DistributedCleanupScope` | experiment-scoped cleanup on a shared queue | cleanup deletes other experiments' jobs too | cleanup is limited to the target experiment |
| `DistributedQueueOwnership` | queue-derived owner display | queued or failed jobs still show stale worker ownership metadata | only actively running jobs expose an owner |
| `DistributedVisibleTrialResults` | final report projection from physical jobs to logical trials | final report counts physical terminal jobs directly | final report collapses to one logical outcome per trial |

## Timeout Recovery Model

`DistributedTimeoutRecovery.tla` is the most complete model in this directory.
It carries both queue state and shadow lifecycle state and is the best entry
point when you want to understand the overall approach.

The buggy config demonstrates this divergence:

- a worker times out
- lifecycle moves back to `queued`
- the real executable queue entry is still absent

The characteristic bad state is:

- `lcState[j] = "queued"`
- `rqState[j] = "rq_absent"`

`HealthySpec` in the same module adds fairness assumptions for the case where at
least one healthy worker and one healthy evaluator exist. Under those
assumptions the healthy config checks stronger properties such as:

- eventual terminal resolution
- terminal-state stickiness
- no duplicate active owner
- artifact/lifecycle consistency
- resume reconciliation completeness
- no false orphaning for healthy workers

## Recommended Run Order

If you want to sanity-check the directory quickly, run these in order:

1. `DistributedTimeoutRecovery.cfg`
2. `DistributedTimeoutRecoveryHealthy.cfg`
3. `DistributedTrialKeySnapshotDuplicate.cfg`
4. `DistributedTrialKeySnapshotUnique.cfg`
5. any specific model that matches the runtime boundary you are editing

For example:

- editing restart logic: start with `DistributedContinueCarryover`, `DistributedResumeArtifacts`, `DistributedResumeCollection`
- editing worker ownership or callbacks: start with `DistributedAttemptOwnership`, `DistributedMonitorCallbacks`, `DistributedLifecycleOwnership`
- editing queue/operator views: start with `DistributedTrialKeySnapshot`, `DistributedQueueOwnership`, `DistributedVisibleTrialResults`

## Scope And Limits

These models are intentionally narrow.

They do not attempt to model:

- full benchmark execution semantics
- the entire Redis schema
- every cloud orchestration step
- arbitrary numbers of workers and controllers

They are useful because each model stays close to one real code path and one
failure mode. When a runtime change modifies one of those boundaries, update the
corresponding model and its healthy/buggy configs together.
