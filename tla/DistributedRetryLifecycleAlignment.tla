---- MODULE DistributedRetryLifecycleAlignment ----
EXTENDS TLC

\* Code-correspondent model for explicit failed-job retry:
\* - a logical trial starts terminal in both physical RQ state and shadow lifecycle
\* - continue mode revives lifecycle before the retry becomes runnable
\* - failed enqueue must roll lifecycle back to failed
\* - active physical attempts and non-terminal lifecycle state must stay aligned

CONSTANT RollbackLifecycleOnEnqueueFailure

QueueStates == {"failed", "queued"}
LifecycleStates == {"failed", "queued"}
EnqueueOutcomes == {"not_attempted", "queued", "enqueue_failed"}

VARIABLES rqState, lcState, enqueueOutcome

vars == <<rqState, lcState, enqueueOutcome>>

Init ==
    /\ rqState = "failed"
    /\ lcState = "failed"
    /\ enqueueOutcome = "not_attempted"

ReviveLifecycleBeforeRetry ==
    /\ rqState = "failed"
    /\ lcState = "failed"
    /\ enqueueOutcome = "not_attempted"
    /\ rqState' = rqState
    /\ lcState' = "queued"
    /\ enqueueOutcome' = enqueueOutcome

ExplicitRetrySucceeds ==
    /\ rqState = "failed"
    /\ lcState = "queued"
    /\ enqueueOutcome = "not_attempted"
    /\ rqState' = "queued"
    /\ lcState' = lcState
    /\ enqueueOutcome' = "queued"

ExplicitRetryFails ==
    /\ rqState = "failed"
    /\ lcState = "queued"
    /\ enqueueOutcome = "not_attempted"
    /\ rqState' = "failed"
    /\ lcState' =
        IF RollbackLifecycleOnEnqueueFailure THEN "failed" ELSE lcState
    /\ enqueueOutcome' = "enqueue_failed"

Next ==
    \/ ReviveLifecycleBeforeRetry
    \/ ExplicitRetrySucceeds
    \/ ExplicitRetryFails
    \/ UNCHANGED vars

TypeInvariant ==
    /\ rqState \in QueueStates
    /\ lcState \in LifecycleStates
    /\ enqueueOutcome \in EnqueueOutcomes

RetriedFailedJobResurrectsLifecycle ==
    rqState = "queued" => lcState = "queued"

ActiveAttemptCardinalityMatchesLifecycle ==
    enqueueOutcome # "not_attempted" => ((rqState = "queued") = (lcState = "queued"))

FailedEnqueueRollsLifecycleBack ==
    enqueueOutcome = "enqueue_failed" => lcState = "failed"

Spec ==
    Init /\ [][Next]_vars

=============================================================================
