---- MODULE DistributedResumeActiveConvergence ----
EXTENDS TLC

\* Code-correspondent model for overlap between resume-only syncing work and an
\* active retry for the same logical trial:
\* - the controller attaches tracked jobs for one monitoring session
\* - a stale syncing attempt is available via resume_collection_job_ids
\* - an active queued/running retry already exists for the same logical trial
\* - the resume-only job must be filtered so it cannot win the canonical marker race

CONSTANT FilterShadowedResumeCandidate

Markers == {"none", "success", "fail"}

VARIABLES attached, trackedResume, marker, activeObserved, resumeObserved

vars == <<attached, trackedResume, marker, activeObserved, resumeObserved>>

Init ==
    /\ attached = FALSE
    /\ trackedResume = FALSE
    /\ marker = "none"
    /\ activeObserved = FALSE
    /\ resumeObserved = FALSE

AttachTrackedJobs ==
    /\ ~attached
    /\ attached' = TRUE
    /\ trackedResume' = ~FilterShadowedResumeCandidate
    /\ UNCHANGED <<marker, activeObserved, resumeObserved>>

ObserveResumeFailure ==
    /\ attached
    /\ trackedResume
    /\ ~resumeObserved
    /\ marker = "none"
    /\ resumeObserved' = TRUE
    /\ marker' = "fail"
    /\ UNCHANGED <<attached, trackedResume, activeObserved>>

ObserveActiveSuccess ==
    /\ attached
    /\ ~activeObserved
    /\ activeObserved' = TRUE
    /\ marker' =
        IF marker = "none" THEN
            "success"
        ELSE
            marker
    /\ UNCHANGED <<attached, trackedResume, resumeObserved>>

Next ==
    \/ AttachTrackedJobs
    \/ ObserveResumeFailure
    \/ ObserveActiveSuccess
    \/ UNCHANGED vars

TypeInvariant ==
    /\ attached \in BOOLEAN
    /\ trackedResume \in BOOLEAN
    /\ marker \in Markers
    /\ activeObserved \in BOOLEAN
    /\ resumeObserved \in BOOLEAN

IfActiveRetryObservedThenVisibleOutcomeSuccess ==
    activeObserved => marker = "success"

Spec ==
    Init /\ [][Next]_vars

=============================================================================
