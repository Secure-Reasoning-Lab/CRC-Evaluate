---- MODULE DistributedMonitorCallbacks ----
EXTENDS TLC

\* Code-correspondent model for orchestrator monitor callbacks after ownership
\* handoff.
\*
\* Python correspondence:
\* - `queue_monitor._process_tracked_jobs()` maintains per-job `seen_finished`
\* - `_build_monitor_callbacks()` in `run_experiment.py` writes orchestrator
\*   markers from finished/failed jobs
\* - finished callbacks fence on the worker identity carried by the terminal
\*   result payload, not mutable shared `job.meta`
\* - the monitor must not consume a stale worker's terminal event after
\*   lifecycle ownership has moved to a replacement worker

CONSTANT FencingEnabled

Workers == {"worker_1", "worker_2"}
Owners == Workers \cup {"none"}
Phases == {"running", "queued"}
MarkerVals == Workers \cup {"none"}

VARIABLES owner, phase, oldFinished, newFinished, seenFinished, markerBy

vars == <<owner, phase, oldFinished, newFinished, seenFinished, markerBy>>

Init ==
    /\ owner = "worker_1"
    /\ phase = "running"
    /\ oldFinished = FALSE
    /\ newFinished = FALSE
    /\ seenFinished = FALSE
    /\ markerBy = "none"

\* Monitor requeues after concluding the old worker is stale.
RecoverOrphaned ==
    /\ phase = "running"
    /\ owner = "worker_1"
    /\ phase' = "queued"
    /\ UNCHANGED <<owner, oldFinished, newFinished, seenFinished, markerBy>>

\* Replacement worker claims the same logical trial.
ClaimReplacement ==
    /\ phase = "queued"
    /\ owner = "worker_1"
    /\ owner' = "worker_2"
    /\ phase' = "running"
    /\ UNCHANGED <<oldFinished, newFinished, seenFinished, markerBy>>

\* Superseded worker eventually finishes, but after ownership has moved.
OldFinishes ==
    /\ phase = "running"
    /\ owner = "worker_2"
    /\ ~oldFinished
    /\ oldFinished' = TRUE
    /\ UNCHANGED <<owner, phase, newFinished, seenFinished, markerBy>>

\* Replacement worker also finishes.
NewFinishes ==
    /\ phase = "running"
    /\ owner = "worker_2"
    /\ ~newFinished
    /\ newFinished' = TRUE
    /\ UNCHANGED <<owner, phase, oldFinished, seenFinished, markerBy>>

\* Queue monitor sees the stale finished event first.
ConsumeOldFinished ==
    /\ oldFinished
    /\ ~seenFinished
    /\ markerBy = "none"
    /\ IF FencingEnabled THEN owner = "worker_1" ELSE TRUE
    /\ seenFinished' = TRUE
    /\ markerBy' = "worker_1"
    /\ UNCHANGED <<owner, phase, oldFinished, newFinished>>

\* Queue monitor accepts the authoritative finished event.
ConsumeNewFinished ==
    /\ newFinished
    /\ ~seenFinished
    /\ markerBy = "none"
    /\ owner = "worker_2"
    /\ seenFinished' = TRUE
    /\ markerBy' = "worker_2"
    /\ UNCHANGED <<owner, phase, oldFinished, newFinished>>

Stutter ==
    UNCHANGED vars

Next ==
    \/ RecoverOrphaned
    \/ ClaimReplacement
    \/ OldFinishes
    \/ NewFinishes
    \/ ConsumeOldFinished
    \/ ConsumeNewFinished
    \/ Stutter

TypeInvariant ==
    /\ owner \in Owners
    /\ phase \in Phases
    /\ oldFinished \in BOOLEAN
    /\ newFinished \in BOOLEAN
    /\ seenFinished \in BOOLEAN
    /\ markerBy \in MarkerVals

StaleOwnerCannotConsumeFinishedCallback ==
    ~(owner = "worker_2" /\ seenFinished /\ markerBy = "worker_1")

AuthoritativeFinishCannotBeMasked ==
    ~(owner = "worker_2" /\ newFinished /\ seenFinished /\ markerBy = "worker_1")

ReplacementOwnerEventuallySetsMarker ==
    <>(markerBy = "worker_2")

Spec ==
    Init /\ [][Next]_vars

HealthySpec ==
    Spec
        /\ WF_vars(RecoverOrphaned)
        /\ WF_vars(ClaimReplacement)
        /\ WF_vars(OldFinishes)
        /\ WF_vars(NewFinishes)
        /\ WF_vars(ConsumeNewFinished)

=============================================================================
