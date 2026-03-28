----------------------------- MODULE DistributedStartupOwnershipFence -----------------------------
EXTENDS TLC

CONSTANT FenceStartupClaim

VARIABLES initialClaimedBy, lifecycleState, claimedBy

Init ==
    /\ lifecycleState = "claimed"
    /\ initialClaimedBy \in {"worker-1", "worker-2"}
    /\ claimedBy = initialClaimedBy

StartWorker ==
    /\ IF FenceStartupClaim
          THEN claimedBy = "worker-1"
          ELSE TRUE
    /\ UNCHANGED initialClaimedBy
    /\ lifecycleState' = "running"
    /\ claimedBy' = "worker-1"

Next ==
    StartWorker
    \/ UNCHANGED <<initialClaimedBy, lifecycleState, claimedBy>>

ForeignClaimNotStolen ==
    ~(
        initialClaimedBy = "worker-2"
        /\ lifecycleState = "running"
        /\ claimedBy = "worker-1"
    )

Spec ==
    Init /\ [][Next]_<<initialClaimedBy, lifecycleState, claimedBy>>

=============================================================================
