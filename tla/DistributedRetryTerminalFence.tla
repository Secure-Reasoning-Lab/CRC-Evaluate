------------------------------ MODULE DistributedRetryTerminalFence ------------------------------
EXTENDS TLC

CONSTANT RetryRequiresFailedLifecycle

VARIABLES physicalState, lifecycleState

Init ==
    /\ physicalState = "failed"
    /\ lifecycleState \in {"failed", "completed"}

RetryExplicitly ==
    /\ physicalState = "failed"
    /\ IF RetryRequiresFailedLifecycle
          THEN lifecycleState = "failed"
          ELSE TRUE
    /\ physicalState' = "queued"
    /\ lifecycleState' =
        IF lifecycleState = "failed"
            THEN "queued"
            ELSE lifecycleState

Next ==
    RetryExplicitly
    \/ UNCHANGED <<physicalState, lifecycleState>>

CompletedLifecycleBlocksRetry ==
    ~(lifecycleState = "completed" /\ physicalState = "queued")

Spec ==
    Init /\ [][Next]_<<physicalState, lifecycleState>>

=============================================================================
