---- MODULE DistributedRetryRefreshBringup ----
EXTENDS TLC

\* Code-correspondent model for continue-mode retry handling:
\* - the controller starts with only failed work in its snapshot
\* - retry requeues that logical trial into the active queue
\* - cloud bring-up is decided from the controller snapshot
\* - the snapshot must be refreshed after retry so queued work is visible

CONSTANT RefreshSnapshotAfterRetry

Snapshots == {"failed_only", "queued_active"}
BringupStates == {"pending", "started", "skipped"}

VARIABLES actualQueue, controllerSnapshot, bringup, retryDone

vars == <<actualQueue, controllerSnapshot, bringup, retryDone>>

Init ==
    /\ actualQueue = "failed_only"
    /\ controllerSnapshot = "failed_only"
    /\ bringup = "pending"
    /\ retryDone = FALSE

RetryFailedJob ==
    /\ retryDone = FALSE
    /\ actualQueue = "failed_only"
    /\ actualQueue' = "queued_active"
    /\ bringup' = bringup
    /\ retryDone' = TRUE
    /\ controllerSnapshot' =
        IF RefreshSnapshotAfterRetry THEN
            "queued_active"
        ELSE
            controllerSnapshot

DecideBringup ==
    /\ retryDone
    /\ bringup = "pending"
    /\ bringup' =
        IF controllerSnapshot = "queued_active" THEN
            "started"
        ELSE
            "skipped"
    /\ UNCHANGED <<actualQueue, controllerSnapshot, retryDone>>

Next ==
    \/ RetryFailedJob
    \/ DecideBringup
    \/ UNCHANGED vars

TypeInvariant ==
    /\ actualQueue \in Snapshots
    /\ controllerSnapshot \in Snapshots
    /\ bringup \in BringupStates
    /\ retryDone \in BOOLEAN

RetriedQueuedWorkStartsBringup ==
    /\ actualQueue = "queued_active"
    /\ bringup # "pending"
    => bringup = "started"

Spec ==
    Init /\ [][Next]_vars

=============================================================================
