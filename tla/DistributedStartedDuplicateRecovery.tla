---- MODULE DistributedStartedDuplicateRecovery ----
EXTENDS Naturals, FiniteSets

\* Code-correspondent model for continue-mode orphan recovery when duplicate
\* physical jobs already exist for one logical trial:
\* - one stale STARTED duplicate is being recovered
\* - another physical job for the same trial is already runnable
\* - recovery must not make both physical jobs runnable

CONSTANT DuplicateAwareRecovery

Jobs == {"job_started", "job_queued"}
States == {"absent", "queued", "started"}

VARIABLES rqState, recovered

vars == <<rqState, recovered>>

Init ==
    /\ rqState =
        [j \in Jobs |->
            IF j = "job_started" THEN "started" ELSE "queued"
        ]
    /\ recovered = FALSE

RecoverStaleStartedDuplicate ==
    /\ rqState["job_started"] = "started"
    /\ rqState["job_queued"] = "queued"
    /\ rqState' =
        IF DuplicateAwareRecovery THEN
            [rqState EXCEPT !["job_started"] = "absent"]
        ELSE
            [rqState EXCEPT !["job_started"] = "queued"]
    /\ recovered' = TRUE

Next ==
    \/ RecoverStaleStartedDuplicate
    \/ UNCHANGED vars

ActiveCount ==
    Cardinality({j \in Jobs : rqState[j] \in {"queued", "started"}})

TypeInvariant ==
    /\ rqState \in [Jobs -> States]
    /\ recovered \in BOOLEAN

AtMostOneActivePhysicalJobForLogicalTrial ==
    recovered => ActiveCount <= 1

Spec ==
    Init /\ [][Next]_vars

=============================================================================
