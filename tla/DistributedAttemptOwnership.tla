---- MODULE DistributedAttemptOwnership ----
EXTENDS TLC

\* Code-correspondent model for stale-worker split brain after orphan recovery.
\*
\* Python correspondence:
\* - worker 1 starts a trial and owns the lifecycle record
\* - monitor falsely concludes worker 1 is orphaned and requeues the same
\*   logical trial
\* - worker 2 claims the requeued attempt
\* - worker 1 may still be alive and finish later
\*
\* Current code correspondence:
\* - lifecycle ownership is tracked via `claimed_by`
\* - stale recovery can move the job back to QUEUED and then new work can be
\*   claimed by a different worker
\* - without a fencing check at publication time, the superseded worker can
\*   still write terminal artifacts

CONSTANT FencingEnabled

Workers == {"worker_1", "worker_2"}
Owners == Workers \cup {"none"}
States == {"running", "queued", "completed"}
PublishedByVals == Workers \cup {"none"}

VARIABLES owner, lcState, oldAlive, newAlive, publishedBy

vars == <<owner, lcState, oldAlive, newAlive, publishedBy>>

Init ==
    /\ owner = "worker_1"
    /\ lcState = "running"
    /\ oldAlive = TRUE
    /\ newAlive = FALSE
    /\ publishedBy = "none"

\* Monitor decides the first worker is stale and makes the logical trial runnable.
RecoverOrphaned ==
    /\ lcState = "running"
    /\ owner = "worker_1"
    /\ oldAlive
    /\ lcState' = "queued"
    /\ UNCHANGED <<owner, oldAlive, newAlive, publishedBy>>

\* A replacement worker claims and starts the retried logical attempt.
ClaimReplacement ==
    /\ lcState = "queued"
    /\ owner = "worker_1"
    /\ oldAlive
    /\ owner' = "worker_2"
    /\ lcState' = "running"
    /\ newAlive' = TRUE
    /\ UNCHANGED <<oldAlive, publishedBy>>

\* Old worker finishes after replacement has already taken ownership.
OldPublishes ==
    /\ oldAlive
    /\ publishedBy = "none"
    /\ IF FencingEnabled THEN owner = "worker_1" ELSE TRUE
    /\ publishedBy' = "worker_1"
    /\ lcState' = "completed"
    /\ oldAlive' = FALSE
    /\ UNCHANGED <<owner, newAlive>>

\* Replacement worker finishes and publishes.
NewPublishes ==
    /\ newAlive
    /\ publishedBy = "none"
    /\ owner = "worker_2"
    /\ publishedBy' = "worker_2"
    /\ lcState' = "completed"
    /\ newAlive' = FALSE
    /\ UNCHANGED <<owner, oldAlive>>

Stutter ==
    UNCHANGED vars

Next ==
    \/ RecoverOrphaned
    \/ ClaimReplacement
    \/ OldPublishes
    \/ NewPublishes
    \/ Stutter

TypeInvariant ==
    /\ owner \in Owners
    /\ lcState \in States
    /\ oldAlive \in BOOLEAN
    /\ newAlive \in BOOLEAN
    /\ publishedBy \in PublishedByVals

SupersededWorkerCannotPublish ==
    ~(owner = "worker_2" /\ publishedBy = "worker_1")

CurrentOwnerPublishesEventually ==
    <>(publishedBy = owner /\ publishedBy # "none")

Spec ==
    Init /\ [][Next]_vars

HealthySpec ==
    Spec
        /\ WF_vars(RecoverOrphaned)
        /\ WF_vars(ClaimReplacement)
        /\ WF_vars(NewPublishes)

=============================================================================
