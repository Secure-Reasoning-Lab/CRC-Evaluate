---- MODULE DistributedRetryLifecycleAlignment ----
EXTENDS TLC

\* Code-correspondent model for explicit failed-job retry:
\* - a logical trial starts terminal in both physical RQ state and shadow lifecycle
\* - continue mode explicitly retries the failed physical job
\* - the shadow lifecycle must be resurrected to queued before the retry runs
\* - active physical attempts and non-terminal lifecycle state must stay aligned

CONSTANT ReviveLifecycleOnRetry

QueueStates == {"failed", "queued"}
LifecycleStates == {"failed", "queued"}

VARIABLES rqState, lcState

vars == <<rqState, lcState>>

Init ==
    /\ rqState = "failed"
    /\ lcState = "failed"

ExplicitRetry ==
    /\ rqState = "failed"
    /\ rqState' = "queued"
    /\ lcState' =
        IF ReviveLifecycleOnRetry THEN
            "queued"
        ELSE
            lcState

Next ==
    \/ ExplicitRetry
    \/ UNCHANGED vars

TypeInvariant ==
    /\ rqState \in QueueStates
    /\ lcState \in LifecycleStates

RetriedFailedJobResurrectsLifecycle ==
    rqState = "queued" => lcState = "queued"

ActiveAttemptCardinalityMatchesLifecycle ==
    (rqState = "queued") = (lcState = "queued")

Spec ==
    Init /\ [][Next]_vars

=============================================================================
