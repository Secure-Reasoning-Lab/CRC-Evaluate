---- MODULE DistributedCleanupScope ----
EXTENDS TLC

\* Code correspondence:
\* - queue cleanup runs against shared queues in distributed/queue_cleanup.py
\* - clear_experiment_jobs() must remove only jobs whose experiment matches the
\*   requested experiment, even when other experiments share the same queue

CONSTANT BuggyUnscopedCleanup

Jobs == {"job_exp_a", "job_exp_b"}

JobExperiment(job) ==
    IF job = "job_exp_a" THEN "exp_a" ELSE "exp_b"

VARIABLES liveJobs, cleanupDone

vars == <<liveJobs, cleanupDone>>

Init ==
    /\ liveJobs = Jobs
    /\ cleanupDone = FALSE

CleanupExpA ==
    /\ ~cleanupDone
    /\ liveJobs' =
        IF BuggyUnscopedCleanup
            THEN {}
            ELSE {job \in liveJobs: JobExperiment(job) # "exp_a"}
    /\ cleanupDone' = TRUE

Stutter ==
    UNCHANGED vars

Next ==
    \/ CleanupExpA
    \/ Stutter

TypeInvariant ==
    /\ liveJobs \subseteq Jobs
    /\ cleanupDone \in BOOLEAN

CleanupScopedToExperiment ==
    cleanupDone => /\ "job_exp_a" \notin liveJobs
                   /\ "job_exp_b" \in liveJobs

Spec ==
    Init /\ [][Next]_vars

=============================================================================
