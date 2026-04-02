---- MODULE DistributedResumeCollection ----
EXTENDS TLC

\* Code-correspondent model for continue-mode restart when only lifecycle
\* syncing work remains.
\*
\* Python correspondence:
\* - `resume_or_raise()` can return job ids still needing collection
\* - there may be no visible queue residue and no new trials to enqueue
\* - continue mode must still resume the stale lock and attach those jobs to the
\*   monitored set instead of exiting early

CONSTANT ResumeCollectionEnabled

VARIABLES lockContended, resumed, needsCollection, trackedJobs, exitedEarly

vars == <<lockContended, resumed, needsCollection, trackedJobs, exitedEarly>>

Init ==
    /\ lockContended = TRUE
    /\ resumed = FALSE
    /\ needsCollection = TRUE
    /\ trackedJobs = 0
    /\ exitedEarly = FALSE

\* Continue mode hits lock contention and must attempt stale-lock resume.
ResumeStaleLock ==
    /\ lockContended
    /\ ~resumed
    /\ resumed' = TRUE
    /\ UNCHANGED <<lockContended, needsCollection, trackedJobs, exitedEarly>>

\* Resumed syncing jobs are attached to the monitor set.
AttachResumeCollectionJobs ==
    /\ resumed
    /\ needsCollection
    /\ trackedJobs = 0
    /\ ResumeCollectionEnabled
    /\ trackedJobs' = 1
    /\ UNCHANGED <<lockContended, resumed, needsCollection, exitedEarly>>

\* Buggy path: controller exits before monitoring resumed collection work.
ExitEarly ==
    /\ needsCollection
    /\ trackedJobs = 0
    /\ IF ResumeCollectionEnabled THEN ~needsCollection ELSE TRUE
    /\ exitedEarly' = TRUE
    /\ UNCHANGED <<lockContended, resumed, needsCollection, trackedJobs>>

Stutter ==
    UNCHANGED vars

Next ==
    \/ ResumeStaleLock
    \/ AttachResumeCollectionJobs
    \/ ExitEarly
    \/ Stutter

TypeInvariant ==
    /\ lockContended \in BOOLEAN
    /\ resumed \in BOOLEAN
    /\ needsCollection \in BOOLEAN
    /\ trackedJobs \in {0, 1}
    /\ exitedEarly \in BOOLEAN

NeedsCollectionPreventsEarlyExit ==
    ~(needsCollection /\ exitedEarly)

ResumeCollectionEventuallyTracked ==
    <>(trackedJobs = 1)

Spec ==
    Init /\ [][Next]_vars

HealthySpec ==
    Spec
        /\ WF_vars(ResumeStaleLock)
        /\ WF_vars(AttachResumeCollectionJobs)

=============================================================================
