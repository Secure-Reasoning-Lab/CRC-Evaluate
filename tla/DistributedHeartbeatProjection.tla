---- MODULE DistributedHeartbeatProjection ----
EXTENDS TLC

\* Code-correspondent model for heartbeat projection on the successful-write path:
\* - a worker emits a lifecycle heartbeat update
\* - the separate heartbeat side channel is refreshed
\* - the lifecycle record must also refresh last_heartbeat / updated_at

CONSTANT SyncLifecycleHeartbeatFields

Markers == {"stale", "fresh"}

VARIABLES heartbeatHash, lifecycleHeartbeat

vars == <<heartbeatHash, lifecycleHeartbeat>>

Init ==
    /\ heartbeatHash = "stale"
    /\ lifecycleHeartbeat = "stale"

HeartbeatUpdate ==
    /\ heartbeatHash = "stale"
    /\ heartbeatHash' = "fresh"
    /\ lifecycleHeartbeat' =
        IF SyncLifecycleHeartbeatFields THEN
            "fresh"
        ELSE
            lifecycleHeartbeat

Next ==
    \/ HeartbeatUpdate
    \/ UNCHANGED vars

TypeInvariant ==
    /\ heartbeatHash \in Markers
    /\ lifecycleHeartbeat \in Markers

LifecycleHeartbeatMatchesSideChannel ==
    heartbeatHash = lifecycleHeartbeat

Spec ==
    Init /\ [][Next]_vars

=============================================================================
