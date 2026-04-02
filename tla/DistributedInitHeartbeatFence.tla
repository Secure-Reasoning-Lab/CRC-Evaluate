------------------------------ MODULE DistributedInitHeartbeatFence ------------------------------
EXTENDS TLC

CONSTANT FenceInitHeartbeat

VARIABLES lifecycleState, claimedBy, initHeartbeatFresh

Init ==
    /\ lifecycleState \in {"running", "completed"}
    /\ claimedBy \in {"worker-1", "worker-2", "none"}
    /\ initHeartbeatFresh = FALSE

InitializeWorker ==
    /\ IF FenceInitHeartbeat
          THEN /\ lifecycleState = "running"
               /\ claimedBy = "worker-1"
          ELSE TRUE
    /\ UNCHANGED <<lifecycleState, claimedBy>>
    /\ initHeartbeatFresh' = TRUE

Next ==
    InitializeWorker
    \/ UNCHANGED <<lifecycleState, claimedBy, initHeartbeatFresh>>

OnlyCurrentOwnerGetsInitHeartbeat ==
    ~(initHeartbeatFresh /\ (lifecycleState = "completed" \/ claimedBy # "worker-1"))

Spec ==
    Init /\ [][Next]_<<lifecycleState, claimedBy, initHeartbeatFresh>>

=============================================================================
