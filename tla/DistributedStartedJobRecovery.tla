---- MODULE DistributedStartedJobRecovery ----
EXTENDS TLC

\* Code-correspondent model for continue-mode started-job recovery.
\*
\* Python correspondence:
\* - `handle_orphaned_jobs()` in `queue.py` inspects STARTED trial jobs
\* - stale recovery should be based on the specific job's timeout-plus-grace
\* - an unrelated live worker on the same queue must not block recovery of a
\*   stale started job whose original owner is gone
\* - once requeued, the shadow lifecycle must also return to `queued` with
\*   ownership cleared so the replacement worker can proceed

CONSTANT PerJobStalenessEnabled, RepairLifecycleOnRequeue

Workers == {"worker_1", "worker_2"}
OwnerVals == Workers \cup {"none"}

VARIABLES ownerAlive, unrelatedWorkerAlive, rqState, lcState, claimedBy, jobAge, blockedByWorkerGate

vars == <<ownerAlive, unrelatedWorkerAlive, rqState, lcState, claimedBy, jobAge, blockedByWorkerGate>>

Init ==
    /\ ownerAlive = FALSE
    /\ unrelatedWorkerAlive = TRUE
    /\ rqState = "started"
    /\ lcState = "running"
    /\ claimedBy = "worker_1"
    /\ jobAge = "stale"
    /\ blockedByWorkerGate = FALSE

\* Buggy gate: any queue worker causes the stale started job to be skipped.
SkipBecauseAnyWorkerExists ==
    /\ rqState = "started"
    /\ jobAge = "stale"
    /\ unrelatedWorkerAlive
    /\ ~blockedByWorkerGate
    /\ ~PerJobStalenessEnabled
    /\ blockedByWorkerGate' = TRUE
    /\ UNCHANGED <<ownerAlive, unrelatedWorkerAlive, rqState, lcState, claimedBy, jobAge>>

\* Fixed behavior: stale started jobs are recovered based on their own age.
RecoverStaleStartedJob ==
    /\ rqState = "started"
    /\ jobAge = "stale"
    /\ IF PerJobStalenessEnabled THEN TRUE ELSE ~unrelatedWorkerAlive
    /\ rqState' = "queued"
    /\ lcState' = IF RepairLifecycleOnRequeue THEN "queued" ELSE lcState
    /\ claimedBy' = IF RepairLifecycleOnRequeue THEN "none" ELSE claimedBy
    /\ blockedByWorkerGate' = FALSE
    /\ UNCHANGED <<ownerAlive, unrelatedWorkerAlive, jobAge>>

Stutter ==
    UNCHANGED vars

Next ==
    \/ SkipBecauseAnyWorkerExists
    \/ RecoverStaleStartedJob
    \/ Stutter

TypeInvariant ==
    /\ ownerAlive \in BOOLEAN
    /\ unrelatedWorkerAlive \in BOOLEAN
    /\ rqState \in {"started", "queued"}
    /\ lcState \in {"running", "queued"}
    /\ claimedBy \in OwnerVals
    /\ jobAge \in {"fresh", "stale"}
    /\ blockedByWorkerGate \in BOOLEAN

UnrelatedWorkerCannotBlockStaleRecovery ==
    ~blockedByWorkerGate

RequeuedStartedJobMatchesLifecycle ==
    rqState = "queued" => /\ lcState = "queued"
                          /\ claimedBy = "none"

StaleStartedJobEventuallyRequeues ==
    <>(rqState = "queued")

Spec ==
    Init /\ [][Next]_vars

HealthySpec ==
    Spec
        /\ WF_vars(RecoverStaleStartedJob)

=============================================================================
