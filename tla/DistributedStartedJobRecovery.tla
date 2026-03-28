---- MODULE DistributedStartedJobRecovery ----
EXTENDS TLC

\* Code-correspondent model for continue-mode started-job recovery.
\*
\* Python correspondence:
\* - `handle_orphaned_jobs()` in `queue.py` inspects STARTED trial jobs
\* - stale recovery should be based on the specific job's timeout-plus-grace
\* - an unrelated live worker on the same queue must not block recovery of a
\*   stale started job whose original owner is gone

CONSTANT PerJobStalenessEnabled

VARIABLES ownerAlive, unrelatedWorkerAlive, jobState, jobAge, blockedByWorkerGate

vars == <<ownerAlive, unrelatedWorkerAlive, jobState, jobAge, blockedByWorkerGate>>

Init ==
    /\ ownerAlive = FALSE
    /\ unrelatedWorkerAlive = TRUE
    /\ jobState = "started"
    /\ jobAge = "stale"
    /\ blockedByWorkerGate = FALSE

\* Buggy gate: any queue worker causes the stale started job to be skipped.
SkipBecauseAnyWorkerExists ==
    /\ jobState = "started"
    /\ jobAge = "stale"
    /\ unrelatedWorkerAlive
    /\ ~blockedByWorkerGate
    /\ ~PerJobStalenessEnabled
    /\ blockedByWorkerGate' = TRUE
    /\ UNCHANGED <<ownerAlive, unrelatedWorkerAlive, jobState, jobAge>>

\* Fixed behavior: stale started jobs are recovered based on their own age.
RecoverStaleStartedJob ==
    /\ jobState = "started"
    /\ jobAge = "stale"
    /\ IF PerJobStalenessEnabled THEN TRUE ELSE ~unrelatedWorkerAlive
    /\ jobState' = "queued"
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
    /\ jobState \in {"started", "queued"}
    /\ jobAge \in {"fresh", "stale"}
    /\ blockedByWorkerGate \in BOOLEAN

UnrelatedWorkerCannotBlockStaleRecovery ==
    ~blockedByWorkerGate

StaleStartedJobEventuallyRequeues ==
    <>(jobState = "queued")

Spec ==
    Init /\ [][Next]_vars

HealthySpec ==
    Spec
        /\ WF_vars(RecoverStaleStartedJob)

=============================================================================
