---- MODULE DistributedMonitorLifecycleGate ----
EXTENDS TLC

\* Code-correspondent model for monitor callbacks after lifecycle state has
\* already gone non-active:
\* - this model abstracts the branch where lifecycle state is readable and a
\*   non-active record is present for the callback's `job_id`
\* - the runtime remains fail-open when lifecycle lookup is unavailable or the
\*   record is missing; that degradation path is outside this model
\* - the lifecycle record is terminal or otherwise non-active
\* - a late finished callback still arrives from RQ
\* - the callback may be consumed so the monitor can make progress
\* - it must not write an orchestrator marker once lifecycle is non-active

CONSTANT GateNonActiveCallbacks

States == {"running", "failed"}

VARIABLES state, callbackConsumed, markerWritten

vars == <<state, callbackConsumed, markerWritten>>

Init ==
    /\ state = "running"
    /\ callbackConsumed = FALSE
    /\ markerWritten = FALSE

LifecycleFails ==
    /\ state = "running"
    /\ state' = "failed"
    /\ UNCHANGED <<callbackConsumed, markerWritten>>

LateFinishedCallback ==
    /\ state = "failed"
    /\ ~callbackConsumed
    /\ callbackConsumed' = TRUE
    /\ markerWritten' =
        IF GateNonActiveCallbacks THEN
            markerWritten
        ELSE
            TRUE
    /\ UNCHANGED state

Next ==
    \/ LifecycleFails
    \/ LateFinishedCallback
    \/ UNCHANGED vars

TypeInvariant ==
    /\ state \in States
    /\ callbackConsumed \in BOOLEAN
    /\ markerWritten \in BOOLEAN

NonActiveLifecycleCannotWriteMarker ==
    state = "failed" => ~markerWritten

LateCallbacksAreConsumed ==
    callbackConsumed
        \/ ~(state = "failed")

Spec ==
    Init /\ [][Next]_vars

=============================================================================
