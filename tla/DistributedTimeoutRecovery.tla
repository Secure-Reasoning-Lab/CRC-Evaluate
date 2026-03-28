---- MODULE DistributedTimeoutRecovery ----
EXTENDS Integers

\* Minimal model of the distributed timeout-recovery path.
\* It intentionally keeps both concrete queue state and shadow lifecycle state
\* because the target bug class is divergence between those two layers.

CONSTANTS Jobs, MaxRetries, BuggyRequeueEnabled

VARIABLES rqState, lcState, retryCount, hbFresh, workerAlive, artifactPublished, graceHits

RQStates == {
    "rq_queued",
    "rq_running",
    "rq_done",
    "rq_failed",
    "rq_absent"
}

LcStates == {
    "queued",
    "claimed",
    "running",
    "syncing",
    "completed",
    "failed",
    "orphaned"
}

vars == <<
    rqState,
    lcState,
    retryCount,
    hbFresh,
    workerAlive,
    artifactPublished,
    graceHits
>>

Init ==
    /\ rqState = [j \in Jobs |-> "rq_queued"]
    /\ lcState = [j \in Jobs |-> "queued"]
    /\ retryCount = [j \in Jobs |-> 0]
    /\ hbFresh = [j \in Jobs |-> TRUE]
    /\ workerAlive = [j \in Jobs |-> FALSE]
    /\ artifactPublished = [j \in Jobs |-> FALSE]
    /\ graceHits = [j \in Jobs |-> 0]

ClaimJob(j) ==
    /\ rqState[j] = "rq_queued"
    /\ lcState[j] = "queued"
    /\ rqState' = [rqState EXCEPT ![j] = "rq_absent"]
    /\ lcState' = [lcState EXCEPT ![j] = "claimed"]
    /\ retryCount' = retryCount
    /\ hbFresh' = [hbFresh EXCEPT ![j] = TRUE]
    /\ workerAlive' = [workerAlive EXCEPT ![j] = TRUE]
    /\ artifactPublished' = artifactPublished
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

StartJob(j) ==
    /\ rqState[j] = "rq_absent"
    /\ lcState[j] = "claimed"
    /\ rqState' = [rqState EXCEPT ![j] = "rq_running"]
    /\ lcState' = [lcState EXCEPT ![j] = "running"]
    /\ retryCount' = retryCount
    /\ hbFresh' = [hbFresh EXCEPT ![j] = TRUE]
    /\ workerAlive' = workerAlive
    /\ artifactPublished' = artifactPublished
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

Heartbeat(j) ==
    /\ lcState[j] \in {"claimed", "running", "syncing"}
    /\ workerAlive[j]
    /\ rqState' = rqState
    /\ lcState' = lcState
    /\ retryCount' = retryCount
    /\ hbFresh' = [hbFresh EXCEPT ![j] = TRUE]
    /\ workerAlive' = workerAlive
    /\ artifactPublished' = artifactPublished
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

StaleHeartbeat(j) ==
    /\ lcState[j] \in {"claimed", "running", "syncing"}
    /\ rqState[j] = "rq_running"
    /\ rqState' = rqState
    /\ lcState' = lcState
    /\ retryCount' = retryCount
    /\ hbFresh' = [hbFresh EXCEPT ![j] = FALSE]
    /\ workerAlive' = workerAlive
    /\ artifactPublished' = artifactPublished
    /\ graceHits' = graceHits

CrashWorker(j) ==
    /\ lcState[j] \in {"claimed", "running", "syncing"}
    /\ rqState[j] = "rq_running"
    /\ workerAlive[j]
    /\ rqState' = rqState
    /\ lcState' = lcState
    /\ retryCount' = retryCount
    /\ hbFresh' = hbFresh
    /\ workerAlive' = [workerAlive EXCEPT ![j] = FALSE]
    /\ artifactPublished' = artifactPublished
    /\ graceHits' = graceHits

EnterSyncing(j) ==
    /\ lcState[j] = "running"
    /\ rqState[j] = "rq_running"
    /\ workerAlive[j]
    /\ rqState' = rqState
    /\ lcState' = [lcState EXCEPT ![j] = "syncing"]
    /\ retryCount' = retryCount
    /\ hbFresh' = [hbFresh EXCEPT ![j] = TRUE]
    /\ workerAlive' = workerAlive
    /\ artifactPublished' = artifactPublished
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

PublishArtifacts(j) ==
    /\ lcState[j] \in {"running", "syncing"}
    /\ rqState[j] = "rq_running"
    /\ rqState' = rqState
    /\ lcState' = lcState
    /\ retryCount' = retryCount
    /\ hbFresh' = hbFresh
    /\ workerAlive' = workerAlive
    /\ artifactPublished' = [artifactPublished EXCEPT ![j] = TRUE]
    /\ graceHits' = graceHits

CompleteJob(j) ==
    /\ lcState[j] = "syncing"
    /\ workerAlive[j]
    /\ artifactPublished[j]
    /\ rqState' = [rqState EXCEPT ![j] = "rq_done"]
    /\ lcState' = [lcState EXCEPT ![j] = "completed"]
    /\ retryCount' = retryCount
    /\ hbFresh' = hbFresh
    /\ workerAlive' = [workerAlive EXCEPT ![j] = FALSE]
    /\ artifactPublished' = artifactPublished
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

ExplicitFail(j) ==
    /\ lcState[j] \in {"claimed", "running", "syncing"}
    /\ rqState' = [rqState EXCEPT ![j] = "rq_failed"]
    /\ lcState' = [lcState EXCEPT ![j] = "failed"]
    /\ retryCount' = retryCount
    /\ hbFresh' = hbFresh
    /\ workerAlive' = [workerAlive EXCEPT ![j] = FALSE]
    /\ artifactPublished' = artifactPublished
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

TimedOutNoWorker(j) ==
    /\ lcState[j] \in {"claimed", "running", "syncing"}
    /\ ~hbFresh[j]
    /\ ~workerAlive[j]
    /\ rqState[j] = "rq_running"

TimeoutScanGrace(j) ==
    /\ TimedOutNoWorker(j)
    /\ graceHits[j] = 0
    /\ rqState' = rqState
    /\ lcState' = lcState
    /\ retryCount' = retryCount
    /\ hbFresh' = hbFresh
    /\ workerAlive' = workerAlive
    /\ artifactPublished' = artifactPublished
    /\ graceHits' = [graceHits EXCEPT ![j] = 1]

TimeoutRecoverToCompletedFromArtifact(j) ==
    /\ TimedOutNoWorker(j)
    /\ graceHits[j] = 1
    /\ artifactPublished[j]
    /\ rqState' = [rqState EXCEPT ![j] = "rq_done"]
    /\ lcState' = [lcState EXCEPT ![j] = "completed"]
    /\ retryCount' = retryCount
    /\ hbFresh' = hbFresh
    /\ workerAlive' = workerAlive
    /\ artifactPublished' = artifactPublished
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

TimeoutRecoverToFailed(j) ==
    /\ TimedOutNoWorker(j)
    /\ graceHits[j] = 1
    /\ ~artifactPublished[j]
    /\ retryCount[j] >= MaxRetries
    /\ rqState' = [rqState EXCEPT ![j] = "rq_failed"]
    /\ lcState' = [lcState EXCEPT ![j] = "failed"]
    /\ retryCount' = retryCount
    /\ hbFresh' = hbFresh
    /\ workerAlive' = workerAlive
    /\ artifactPublished' = artifactPublished
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

TimeoutRecoverToQueuedIntended(j) ==
    /\ TimedOutNoWorker(j)
    /\ graceHits[j] = 1
    /\ ~artifactPublished[j]
    /\ retryCount[j] < MaxRetries
    /\ ~BuggyRequeueEnabled
    /\ rqState' = [rqState EXCEPT ![j] = "rq_queued"]
    /\ lcState' = [lcState EXCEPT ![j] = "queued"]
    /\ retryCount' = [retryCount EXCEPT ![j] = @ + 1]
    /\ hbFresh' = hbFresh
    /\ workerAlive' = workerAlive
    /\ artifactPublished' = artifactPublished
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

TimeoutRecoverToQueuedBuggy(j) ==
    /\ TimedOutNoWorker(j)
    /\ graceHits[j] = 1
    /\ ~artifactPublished[j]
    /\ retryCount[j] < MaxRetries
    /\ BuggyRequeueEnabled
    /\ rqState' = [rqState EXCEPT ![j] = "rq_absent"]
    /\ lcState' = [lcState EXCEPT ![j] = "queued"]
    /\ retryCount' = [retryCount EXCEPT ![j] = @ + 1]
    /\ hbFresh' = hbFresh
    /\ workerAlive' = workerAlive
    /\ artifactPublished' = artifactPublished
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

Next ==
    \E j \in Jobs :
        \/ ClaimJob(j)
        \/ StartJob(j)
        \/ Heartbeat(j)
        \/ StaleHeartbeat(j)
        \/ CrashWorker(j)
        \/ EnterSyncing(j)
        \/ PublishArtifacts(j)
        \/ CompleteJob(j)
        \/ ExplicitFail(j)
        \/ TimeoutScanGrace(j)
        \/ TimeoutRecoverToCompletedFromArtifact(j)
        \/ TimeoutRecoverToFailed(j)
        \/ TimeoutRecoverToQueuedIntended(j)
        \/ TimeoutRecoverToQueuedBuggy(j)

TypeInvariant ==
    /\ rqState \in [Jobs -> RQStates]
    /\ lcState \in [Jobs -> LcStates]
    /\ retryCount \in [Jobs -> 0..(MaxRetries + 1)]
    /\ hbFresh \in [Jobs -> BOOLEAN]
    /\ workerAlive \in [Jobs -> BOOLEAN]
    /\ artifactPublished \in [Jobs -> BOOLEAN]
    /\ graceHits \in [Jobs -> 0..1]

QueuedMeansExecutable ==
    \A j \in Jobs :
        lcState[j] = "queued" => rqState[j] = "rq_queued"

TerminalStatesConsistent ==
    \A j \in Jobs :
        /\ (lcState[j] = "completed" => rqState[j] \in {"rq_done", "rq_absent"})
        /\ (lcState[j] = "failed" => rqState[j] \in {"rq_failed", "rq_absent"})

NoStalledTimedOutJob ==
    \A j \in Jobs :
        ~(lcState[j] = "queued" /\ rqState[j] = "rq_absent")

EventuallyResolvedAfterTimeout ==
    \A j \in Jobs :
        [](
            TimedOutNoWorker(j) =>
                <>(
                    /\ lcState[j] \in {"queued", "completed", "failed"}
                    /\ rqState[j] # "rq_running"
                )
        )

EventuallyCompletedOrFailed ==
    \A j \in Jobs :
        <>(lcState[j] \in {"completed", "failed"})

TerminalLifecycleSticky ==
    \A j \in Jobs :
        [](
            lcState[j] \in {"completed", "failed"} =>
                [](
                    /\ lcState[j] \in {"completed", "failed"}
                    /\ (lcState[j] = "completed" => rqState[j] = "rq_done")
                    /\ (lcState[j] = "failed" => rqState[j] = "rq_failed")
                )
        )

AdvanceRunningOrClaimed(j) ==
    EnterSyncing(j) \/ ExplicitFail(j)

AdvanceSyncing(j) ==
    PublishArtifacts(j) \/ CompleteJob(j) \/ ExplicitFail(j)

TimeoutRecover(j) ==
    TimeoutRecoverToCompletedFromArtifact(j)
        \/ TimeoutRecoverToFailed(j)
        \/ TimeoutRecoverToQueuedIntended(j)

\* Assumption bundle for the "healthy infra exists" model:
\* - at least one healthy worker eventually claims queued work
\* - started work eventually either advances toward success or explicitly fails
\* - if a worker dies, heartbeat staleness and timeout recovery are eventually observed
\* - a healthy evaluator eventually publishes artifacts for successful runs
HealthyInfraFairness ==
    \A j \in Jobs :
        /\ WF_vars(ClaimJob(j))
        /\ WF_vars(StartJob(j))
        /\ WF_vars(StaleHeartbeat(j))
        /\ WF_vars(AdvanceRunningOrClaimed(j))
        /\ WF_vars(AdvanceSyncing(j))
        /\ WF_vars(TimeoutScanGrace(j))
        /\ WF_vars(TimeoutRecover(j))

Spec == Init /\ [][Next]_vars

HealthySpec ==
    /\ Init
    /\ [][Next]_vars
    /\ ~BuggyRequeueEnabled
    /\ HealthyInfraFairness

=============================================================================
