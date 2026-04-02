---- MODULE DistributedContinueCarryover ----
EXTENDS TLC

\* Code-correspondent model for `crsbench run --queue-mode continue` when the
\* queue already contains a terminal physical job for the requested logical
\* trial, but the orchestrator marker has not yet been written.
\*
\* Python correspondence:
\* - continue mode skips re-enqueue because `get_existing_trials()` reports the
\*   logical trial key as already present
\* - the controller must still attach the existing physical RQ job to
\*   `monitor_jobs()` so callbacks can write `.success` / `.fail`
\* - exiting without attaching silently drops a terminal carryover result

CONSTANTS AttachCarryoverJobs, CarryoverFails

QueueStates == {"finished", "failed"}
Markers == {"absent", "success", "fail"}

ExpectedMarker ==
    IF CarryoverFails THEN "fail" ELSE "success"

VARIABLES rqState, marker, tracked, monitoring, done

vars == <<rqState, marker, tracked, monitoring, done>>

Init ==
    /\ rqState = IF CarryoverFails THEN "failed" ELSE "finished"
    /\ marker = "absent"
    /\ tracked = FALSE
    /\ monitoring = FALSE
    /\ done = FALSE

\* Continue-mode skip decision: do not enqueue new work for this logical trial.
\* The only safe path is to attach the carryover job for monitoring.
ContinueSkipAndDecide ==
    /\ ~monitoring
    /\ ~done
    /\ marker = "absent"
    /\ tracked' = AttachCarryoverJobs
    /\ monitoring' = AttachCarryoverJobs
    /\ done' = ~AttachCarryoverJobs
    /\ UNCHANGED <<rqState, marker>>

\* monitor_jobs() callback writes the missing orchestrator marker.
CollectTrackedTerminal ==
    /\ monitoring
    /\ ~done
    /\ marker = "absent"
    /\ marker' = ExpectedMarker
    /\ UNCHANGED <<rqState, tracked, monitoring, done>>

\* Controller exits only after the carryover result has been materialized.
ExitAfterCollection ==
    /\ monitoring
    /\ ~done
    /\ marker = ExpectedMarker
    /\ monitoring' = FALSE
    /\ done' = TRUE
    /\ UNCHANGED <<rqState, marker, tracked>>

Stutter ==
    UNCHANGED vars

Next ==
    \/ ContinueSkipAndDecide
    \/ CollectTrackedTerminal
    \/ ExitAfterCollection
    \/ Stutter

TypeInvariant ==
    /\ rqState \in QueueStates
    /\ marker \in Markers
    /\ tracked \in BOOLEAN
    /\ monitoring \in BOOLEAN
    /\ done \in BOOLEAN

MarkerMatchesTerminalState ==
    marker = "absent" \/ marker = ExpectedMarker

DoneImpliesMarker ==
    done => marker = ExpectedMarker

CarryoverEventuallyMarked ==
    <>(marker = ExpectedMarker)

EventuallyDone ==
    <>done

Spec ==
    Init /\ [][Next]_vars

HealthySpec ==
    Spec
        /\ WF_vars(ContinueSkipAndDecide)
        /\ WF_vars(CollectTrackedTerminal)
        /\ WF_vars(ExitAfterCollection)

=============================================================================
