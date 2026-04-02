---- MODULE DistributedRetryMetadataProjection ----
EXTENDS Naturals, TLC

\* Code-correspondent model for retry-count projection after orphan recovery on
\* the successful metadata-write path:
\* - orphan recovery requeues a concrete RQ job
\* - lifecycle retry_count increments
\* - queue-derived operator views read retry_count from RQ metadata
\* - the concrete metadata must be updated to match the lifecycle counter

CONSTANT SyncRQRetryMetadata

VARIABLES lifecycleRetry, rqRetry

vars == <<lifecycleRetry, rqRetry>>

Init ==
    /\ lifecycleRetry = 0
    /\ rqRetry = 0

RecoverAndRequeue ==
    /\ lifecycleRetry = 0
    /\ rqRetry = 0
    /\ lifecycleRetry' = lifecycleRetry + 1
    /\ rqRetry' =
        IF SyncRQRetryMetadata THEN
            lifecycleRetry + 1
        ELSE
            rqRetry

Next ==
    \/ RecoverAndRequeue
    \/ UNCHANGED vars

TypeInvariant ==
    /\ lifecycleRetry \in Nat
    /\ rqRetry \in Nat

LifecycleRetryMatchesRQMetadata ==
    lifecycleRetry = rqRetry

Spec ==
    Init /\ [][Next]_vars

=============================================================================
