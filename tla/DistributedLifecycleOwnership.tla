---- MODULE DistributedLifecycleOwnership ----
EXTENDS TLC

\* Code-correspondent model for lifecycle ownership clearing:
\* - a worker owns an active running attempt
\* - recovery or terminalization moves the job into a non-active state
\* - non-active states must clear claimed_by so stale workers lose authority

CONSTANT ClearOwnerOnNonActiveTransition

States == {"running", "orphaned", "queued", "failed", "completed"}
Owners == {"worker-1", "none"}
NonActiveStates == {"orphaned", "queued", "failed", "completed"}

VARIABLES state, owner, stalePublished

vars == <<state, owner, stalePublished>>

Init ==
    /\ state = "running"
    /\ owner = "worker-1"
    /\ stalePublished = FALSE

RecoverToOrphaned ==
    /\ state = "running"
    /\ state' = "orphaned"
    /\ owner' =
        IF ClearOwnerOnNonActiveTransition THEN
            "none"
        ELSE
            owner
    /\ UNCHANGED stalePublished

RetryToQueued ==
    /\ state = "orphaned"
    /\ state' = "queued"
    /\ owner' =
        IF ClearOwnerOnNonActiveTransition THEN
            "none"
        ELSE
            owner
    /\ UNCHANGED stalePublished

FailTerminal ==
    /\ state = "running"
    /\ state' = "failed"
    /\ owner' =
        IF ClearOwnerOnNonActiveTransition THEN
            "none"
        ELSE
            owner
    /\ UNCHANGED stalePublished

CompleteTerminal ==
    /\ state = "running"
    /\ state' = "completed"
    /\ owner' =
        IF ClearOwnerOnNonActiveTransition THEN
            "none"
        ELSE
            owner
    /\ UNCHANGED stalePublished

StaleWorkerPublishes ==
    /\ state \in NonActiveStates
    /\ owner = "worker-1"
    /\ ~stalePublished
    /\ stalePublished' = TRUE
    /\ UNCHANGED <<state, owner>>

Next ==
    \/ RecoverToOrphaned
    \/ RetryToQueued
    \/ FailTerminal
    \/ CompleteTerminal
    \/ StaleWorkerPublishes
    \/ UNCHANGED vars

TypeInvariant ==
    /\ state \in States
    /\ owner \in Owners
    /\ stalePublished \in BOOLEAN

NonActiveStatesAreOwnerless ==
    state \in NonActiveStates => owner = "none"

StaleWorkersCannotPublishAfterRecovery ==
    ~stalePublished

Spec ==
    Init /\ [][Next]_vars

=============================================================================
