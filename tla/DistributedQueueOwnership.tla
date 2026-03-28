---- MODULE DistributedQueueOwnership ----
EXTENDS TLC

\* Code-correspondent model for queue-monitor ownership display vs shadow
\* lifecycle ownership.
\*
\* Python correspondence:
\* - workers write `job.meta["worker_name"]` when they start a trial
\* - retry/requeue may leave that metadata behind on a queued or failed RQ job
\* - `queue_monitor.list_queue_job_entries()` must not present stale metadata as
\*   an active owner for non-running jobs

CONSTANT PreserveStaleWorkerMeta

Workers == {"worker_1", "worker_2"}
OwnerVals == Workers \cup {"none"}
RQStates == {"queued", "running", "failed"}
LCStates == {"queued", "running", "failed"}

VARIABLES rqState, rqWorkerMeta, lcState, claimedBy

vars == <<rqState, rqWorkerMeta, lcState, claimedBy>>

Init ==
    /\ rqState = "queued"
    /\ rqWorkerMeta = "none"
    /\ lcState = "queued"
    /\ claimedBy = "none"

StartOnWorker1 ==
    /\ rqState = "queued"
    /\ lcState = "queued"
    /\ rqState' = "running"
    /\ rqWorkerMeta' = "worker_1"
    /\ lcState' = "running"
    /\ claimedBy' = "worker_1"

RecoverToQueued ==
    /\ rqState = "running"
    /\ lcState = "running"
    /\ rqState' = "queued"
    /\ rqWorkerMeta' = IF PreserveStaleWorkerMeta THEN rqWorkerMeta ELSE "none"
    /\ lcState' = "queued"
    /\ claimedBy' = "none"

FailAttempt ==
    /\ rqState = "running"
    /\ lcState = "running"
    /\ rqState' = "failed"
    /\ rqWorkerMeta' = IF PreserveStaleWorkerMeta THEN rqWorkerMeta ELSE "none"
    /\ lcState' = "failed"
    /\ claimedBy' = "none"

StartOnWorker2 ==
    /\ rqState = "queued"
    /\ lcState = "queued"
    /\ rqState' = "running"
    /\ rqWorkerMeta' = "worker_2"
    /\ lcState' = "running"
    /\ claimedBy' = "worker_2"

Stutter ==
    UNCHANGED vars

Next ==
    \/ StartOnWorker1
    \/ RecoverToQueued
    \/ FailAttempt
    \/ StartOnWorker2
    \/ Stutter

TypeInvariant ==
    /\ rqState \in RQStates
    /\ rqWorkerMeta \in OwnerVals
    /\ lcState \in LCStates
    /\ claimedBy \in OwnerVals

NonRunningQueueHasNoOwner ==
    rqState # "running" => rqWorkerMeta = "none"

RunningQueueMatchesLifecycleOwner ==
    rqState = "running" => /\ claimedBy = rqWorkerMeta
                           /\ claimedBy # "none"
                           /\ lcState = "running"

LifecycleMatchesRQOwnership ==
    /\ NonRunningQueueHasNoOwner
    /\ RunningQueueMatchesLifecycleOwner

Spec ==
    Init /\ [][Next]_vars

=============================================================================
