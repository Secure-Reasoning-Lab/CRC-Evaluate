---- MODULE DistributedMonitorMarkerWrite ----
EXTENDS TLC

\* Code-correspondent model for orchestrator marker-write retries.
\*
\* Python correspondence:
\* - `_build_monitor_callbacks()` writes `.success` / `.fail` markers
\* - `_process_tracked_jobs()` only advances `seen_finished` when the callback
\*   accepts the terminal event
\* - transient marker-write failures must leave the terminal event retryable

CONSTANT RetryOnFailureEnabled

VARIABLES finished, failurePending, seenFinished, markerWritten

vars == <<finished, failurePending, seenFinished, markerWritten>>

Init ==
    /\ finished = TRUE
    /\ failurePending = TRUE
    /\ seenFinished = FALSE
    /\ markerWritten = FALSE

\* First callback attempt hits a transient filesystem or storage failure.
MarkerWriteFails ==
    /\ finished
    /\ failurePending
    /\ ~markerWritten
    /\ ~seenFinished
    /\ failurePending' = FALSE
    /\ markerWritten' = FALSE
    /\ IF RetryOnFailureEnabled THEN seenFinished' = FALSE ELSE seenFinished' = TRUE
    /\ UNCHANGED finished

\* A later callback retry succeeds.
MarkerWriteSucceeds ==
    /\ finished
    /\ ~failurePending
    /\ ~markerWritten
    /\ ~seenFinished
    /\ markerWritten' = TRUE
    /\ seenFinished' = TRUE
    /\ UNCHANGED <<finished, failurePending>>

Stutter ==
    UNCHANGED vars

Next ==
    \/ MarkerWriteFails
    \/ MarkerWriteSucceeds
    \/ Stutter

TypeInvariant ==
    /\ finished \in BOOLEAN
    /\ failurePending \in BOOLEAN
    /\ seenFinished \in BOOLEAN
    /\ markerWritten \in BOOLEAN

WriteFailureCannotConsumeFinishedJob ==
    ~(seenFinished /\ ~markerWritten)

FinishedJobEventuallyWritesMarker ==
    <>(markerWritten)

Spec ==
    Init /\ [][Next]_vars

HealthySpec ==
    Spec
        /\ WF_vars(MarkerWriteFails)
        /\ WF_vars(MarkerWriteSucceeds)

=============================================================================
