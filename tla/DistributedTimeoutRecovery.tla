---- MODULE DistributedTimeoutRecovery ----
EXTENDS Integers

\* Minimal model of the distributed timeout-recovery path.
\* It intentionally keeps both concrete queue state and shadow lifecycle state
\* because the target bug class is divergence between those two layers.

CONSTANTS Jobs, MaxRetries, BuggyRequeueEnabled

VARIABLES
    rqState,
    lcState,
    retryCount,
    requeueCount,
    hbFresh,
    workerAlive,
    claimedBy,
    artifactState,
    needsCollection,
    graceHits

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

Claimants == {
    "none",
    "worker"
}

ArtifactStates == {
    "none",
    "success",
    "fail"
}

vars == <<
    rqState,
    lcState,
    retryCount,
    requeueCount,
    hbFresh,
    workerAlive,
    claimedBy,
    artifactState,
    needsCollection,
    graceHits
>>

Init ==
    /\ rqState = [j \in Jobs |-> "rq_queued"]
    /\ lcState = [j \in Jobs |-> "queued"]
    /\ retryCount = [j \in Jobs |-> 0]
    /\ requeueCount = [j \in Jobs |-> 0]
    /\ hbFresh = [j \in Jobs |-> TRUE]
    /\ workerAlive = [j \in Jobs |-> FALSE]
    /\ claimedBy = [j \in Jobs |-> "none"]
    /\ artifactState = [j \in Jobs |-> "none"]
    /\ needsCollection = [j \in Jobs |-> FALSE]
    /\ graceHits = [j \in Jobs |-> 0]

ClaimJob(j) ==
    /\ rqState[j] = "rq_queued"
    /\ lcState[j] = "queued"
    /\ claimedBy[j] = "none"
    /\ rqState' = [rqState EXCEPT ![j] = "rq_absent"]
    /\ lcState' = [lcState EXCEPT ![j] = "claimed"]
    /\ retryCount' = retryCount
    /\ requeueCount' = requeueCount
    /\ hbFresh' = [hbFresh EXCEPT ![j] = TRUE]
    /\ workerAlive' = [workerAlive EXCEPT ![j] = TRUE]
    /\ claimedBy' = [claimedBy EXCEPT ![j] = "worker"]
    /\ artifactState' = artifactState
    /\ needsCollection' = [needsCollection EXCEPT ![j] = FALSE]
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

StartJob(j) ==
    /\ rqState[j] = "rq_absent"
    /\ lcState[j] = "claimed"
    /\ claimedBy[j] = "worker"
    /\ rqState' = [rqState EXCEPT ![j] = "rq_running"]
    /\ lcState' = [lcState EXCEPT ![j] = "running"]
    /\ retryCount' = retryCount
    /\ requeueCount' = requeueCount
    /\ hbFresh' = [hbFresh EXCEPT ![j] = TRUE]
    /\ workerAlive' = workerAlive
    /\ claimedBy' = claimedBy
    /\ artifactState' = artifactState
    /\ needsCollection' = needsCollection
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

Heartbeat(j) ==
    /\ lcState[j] \in {"claimed", "running", "syncing"}
    /\ workerAlive[j]
    /\ claimedBy[j] = "worker"
    /\ rqState' = rqState
    /\ lcState' = lcState
    /\ retryCount' = retryCount
    /\ requeueCount' = requeueCount
    /\ hbFresh' = [hbFresh EXCEPT ![j] = TRUE]
    /\ workerAlive' = workerAlive
    /\ claimedBy' = claimedBy
    /\ artifactState' = artifactState
    /\ needsCollection' = needsCollection
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

StaleHeartbeat(j) ==
    /\ lcState[j] \in {"claimed", "running", "syncing"}
    /\ rqState[j] = "rq_running"
    /\ claimedBy[j] = "worker"
    /\ rqState' = rqState
    /\ lcState' = lcState
    /\ retryCount' = retryCount
    /\ requeueCount' = requeueCount
    /\ hbFresh' = [hbFresh EXCEPT ![j] = FALSE]
    /\ workerAlive' = workerAlive
    /\ claimedBy' = claimedBy
    /\ artifactState' = artifactState
    /\ needsCollection' = needsCollection
    /\ graceHits' = graceHits

CrashWorker(j) ==
    /\ lcState[j] \in {"claimed", "running", "syncing"}
    /\ rqState[j] = "rq_running"
    /\ workerAlive[j]
    /\ claimedBy[j] = "worker"
    /\ rqState' = rqState
    /\ lcState' = lcState
    /\ retryCount' = retryCount
    /\ requeueCount' = requeueCount
    /\ hbFresh' = hbFresh
    /\ workerAlive' = [workerAlive EXCEPT ![j] = FALSE]
    /\ claimedBy' = claimedBy
    /\ artifactState' = artifactState
    /\ needsCollection' = needsCollection
    /\ graceHits' = graceHits

EnterSyncing(j) ==
    /\ lcState[j] = "running"
    /\ rqState[j] = "rq_running"
    /\ workerAlive[j]
    /\ claimedBy[j] = "worker"
    /\ rqState' = rqState
    /\ lcState' = [lcState EXCEPT ![j] = "syncing"]
    /\ retryCount' = retryCount
    /\ requeueCount' = requeueCount
    /\ hbFresh' = [hbFresh EXCEPT ![j] = TRUE]
    /\ workerAlive' = workerAlive
    /\ claimedBy' = claimedBy
    /\ artifactState' = artifactState
    /\ needsCollection' = [needsCollection EXCEPT ![j] = TRUE]
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

PublishSuccessArtifacts(j) ==
    /\ lcState[j] = "syncing"
    /\ rqState[j] = "rq_running"
    /\ workerAlive[j]
    /\ claimedBy[j] = "worker"
    /\ artifactState[j] = "none"
    /\ rqState' = rqState
    /\ lcState' = lcState
    /\ retryCount' = retryCount
    /\ requeueCount' = requeueCount
    /\ hbFresh' = hbFresh
    /\ workerAlive' = workerAlive
    /\ claimedBy' = claimedBy
    /\ artifactState' = [artifactState EXCEPT ![j] = "success"]
    /\ needsCollection' = needsCollection
    /\ graceHits' = graceHits

CompleteJob(j) ==
    /\ lcState[j] = "syncing"
    /\ workerAlive[j]
    /\ artifactState[j] = "success"
    /\ rqState' = [rqState EXCEPT ![j] = "rq_done"]
    /\ lcState' = [lcState EXCEPT ![j] = "completed"]
    /\ retryCount' = retryCount
    /\ requeueCount' = requeueCount
    /\ hbFresh' = hbFresh
    /\ workerAlive' = [workerAlive EXCEPT ![j] = FALSE]
    /\ claimedBy' = [claimedBy EXCEPT ![j] = "none"]
    /\ artifactState' = artifactState
    /\ needsCollection' = [needsCollection EXCEPT ![j] = FALSE]
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

ExplicitFail(j) ==
    /\ lcState[j] \in {"claimed", "running", "syncing"}
    /\ artifactState[j] = "none"
    /\ rqState' = [rqState EXCEPT ![j] = "rq_failed"]
    /\ lcState' = [lcState EXCEPT ![j] = "failed"]
    /\ retryCount' = retryCount
    /\ requeueCount' = requeueCount
    /\ hbFresh' = hbFresh
    /\ workerAlive' = [workerAlive EXCEPT ![j] = FALSE]
    /\ claimedBy' = [claimedBy EXCEPT ![j] = "none"]
    /\ artifactState' = [artifactState EXCEPT ![j] = "fail"]
    /\ needsCollection' = [needsCollection EXCEPT ![j] = FALSE]
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

TimedOutNoWorker(j) ==
    /\ lcState[j] \in {"claimed", "running", "syncing"}
    /\ ~hbFresh[j]
    /\ ~workerAlive[j]
    /\ claimedBy[j] = "worker"
    /\ rqState[j] = "rq_running"

TimeoutScanGrace(j) ==
    /\ TimedOutNoWorker(j)
    /\ graceHits[j] = 0
    /\ rqState' = rqState
    /\ lcState' = lcState
    /\ retryCount' = retryCount
    /\ requeueCount' = requeueCount
    /\ hbFresh' = hbFresh
    /\ workerAlive' = workerAlive
    /\ claimedBy' = claimedBy
    /\ artifactState' = artifactState
    /\ needsCollection' = needsCollection
    /\ graceHits' = [graceHits EXCEPT ![j] = 1]

TimeoutRecoverToCompletedFromArtifact(j) ==
    /\ TimedOutNoWorker(j)
    /\ graceHits[j] = 1
    /\ artifactState[j] = "success"
    /\ rqState' = [rqState EXCEPT ![j] = "rq_done"]
    /\ lcState' = [lcState EXCEPT ![j] = "completed"]
    /\ retryCount' = retryCount
    /\ requeueCount' = requeueCount
    /\ hbFresh' = hbFresh
    /\ workerAlive' = workerAlive
    /\ claimedBy' = [claimedBy EXCEPT ![j] = "none"]
    /\ artifactState' = artifactState
    /\ needsCollection' = [needsCollection EXCEPT ![j] = FALSE]
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

TimeoutRecoverToFailedFromArtifact(j) ==
    /\ TimedOutNoWorker(j)
    /\ graceHits[j] = 1
    /\ artifactState[j] = "fail"
    /\ rqState' = [rqState EXCEPT ![j] = "rq_failed"]
    /\ lcState' = [lcState EXCEPT ![j] = "failed"]
    /\ retryCount' = retryCount
    /\ requeueCount' = requeueCount
    /\ hbFresh' = hbFresh
    /\ workerAlive' = workerAlive
    /\ claimedBy' = [claimedBy EXCEPT ![j] = "none"]
    /\ artifactState' = artifactState
    /\ needsCollection' = [needsCollection EXCEPT ![j] = FALSE]
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

TimeoutRecoverToFailed(j) ==
    /\ TimedOutNoWorker(j)
    /\ graceHits[j] = 1
    /\ artifactState[j] = "none"
    /\ retryCount[j] >= MaxRetries
    /\ rqState' = [rqState EXCEPT ![j] = "rq_failed"]
    /\ lcState' = [lcState EXCEPT ![j] = "failed"]
    /\ retryCount' = retryCount
    /\ requeueCount' = requeueCount
    /\ hbFresh' = hbFresh
    /\ workerAlive' = workerAlive
    /\ claimedBy' = [claimedBy EXCEPT ![j] = "none"]
    /\ artifactState' = artifactState
    /\ needsCollection' = [needsCollection EXCEPT ![j] = FALSE]
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

TimeoutRecoverToQueuedIntended(j) ==
    /\ TimedOutNoWorker(j)
    /\ graceHits[j] = 1
    /\ artifactState[j] = "none"
    /\ retryCount[j] < MaxRetries
    /\ ~BuggyRequeueEnabled
    /\ rqState' = [rqState EXCEPT ![j] = "rq_queued"]
    /\ lcState' = [lcState EXCEPT ![j] = "queued"]
    /\ retryCount' = [retryCount EXCEPT ![j] = @ + 1]
    /\ requeueCount' = [requeueCount EXCEPT ![j] = @ + 1]
    /\ hbFresh' = [hbFresh EXCEPT ![j] = TRUE]
    /\ workerAlive' = workerAlive
    /\ claimedBy' = [claimedBy EXCEPT ![j] = "none"]
    /\ artifactState' = artifactState
    /\ needsCollection' = [needsCollection EXCEPT ![j] = FALSE]
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

TimeoutRecoverToQueuedBuggy(j) ==
    /\ TimedOutNoWorker(j)
    /\ graceHits[j] = 1
    /\ artifactState[j] = "none"
    /\ retryCount[j] < MaxRetries
    /\ BuggyRequeueEnabled
    /\ rqState' = [rqState EXCEPT ![j] = "rq_absent"]
    /\ lcState' = [lcState EXCEPT ![j] = "queued"]
    /\ retryCount' = [retryCount EXCEPT ![j] = @ + 1]
    /\ requeueCount' = [requeueCount EXCEPT ![j] = @ + 1]
    /\ hbFresh' = [hbFresh EXCEPT ![j] = TRUE]
    /\ workerAlive' = workerAlive
    /\ claimedBy' = [claimedBy EXCEPT ![j] = "none"]
    /\ artifactState' = artifactState
    /\ needsCollection' = [needsCollection EXCEPT ![j] = FALSE]
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

ResumeReconcileSuccess(j) ==
    /\ lcState[j] = "syncing"
    /\ needsCollection[j]
    /\ artifactState[j] = "success"
    /\ rqState[j] = "rq_running"
    /\ rqState' = [rqState EXCEPT ![j] = "rq_done"]
    /\ lcState' = [lcState EXCEPT ![j] = "completed"]
    /\ retryCount' = retryCount
    /\ requeueCount' = requeueCount
    /\ hbFresh' = hbFresh
    /\ workerAlive' = workerAlive
    /\ claimedBy' = [claimedBy EXCEPT ![j] = "none"]
    /\ artifactState' = artifactState
    /\ needsCollection' = [needsCollection EXCEPT ![j] = FALSE]
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

ResumeReconcileFailure(j) ==
    /\ lcState[j] = "syncing"
    /\ needsCollection[j]
    /\ artifactState[j] = "fail"
    /\ rqState[j] = "rq_running"
    /\ rqState' = [rqState EXCEPT ![j] = "rq_failed"]
    /\ lcState' = [lcState EXCEPT ![j] = "failed"]
    /\ retryCount' = retryCount
    /\ requeueCount' = requeueCount
    /\ hbFresh' = hbFresh
    /\ workerAlive' = workerAlive
    /\ claimedBy' = [claimedBy EXCEPT ![j] = "none"]
    /\ artifactState' = artifactState
    /\ needsCollection' = [needsCollection EXCEPT ![j] = FALSE]
    /\ graceHits' = [graceHits EXCEPT ![j] = 0]

Next ==
    \E j \in Jobs :
        \/ ClaimJob(j)
        \/ StartJob(j)
        \/ Heartbeat(j)
        \/ StaleHeartbeat(j)
        \/ CrashWorker(j)
        \/ EnterSyncing(j)
        \/ PublishSuccessArtifacts(j)
        \/ CompleteJob(j)
        \/ ExplicitFail(j)
        \/ TimeoutScanGrace(j)
        \/ TimeoutRecoverToCompletedFromArtifact(j)
        \/ TimeoutRecoverToFailedFromArtifact(j)
        \/ TimeoutRecoverToFailed(j)
        \/ TimeoutRecoverToQueuedIntended(j)
        \/ TimeoutRecoverToQueuedBuggy(j)
        \/ ResumeReconcileSuccess(j)
        \/ ResumeReconcileFailure(j)

TypeInvariant ==
    /\ rqState \in [Jobs -> RQStates]
    /\ lcState \in [Jobs -> LcStates]
    /\ retryCount \in [Jobs -> 0..(MaxRetries + 1)]
    /\ requeueCount \in [Jobs -> 0..(MaxRetries + 1)]
    /\ hbFresh \in [Jobs -> BOOLEAN]
    /\ workerAlive \in [Jobs -> BOOLEAN]
    /\ claimedBy \in [Jobs -> Claimants]
    /\ artifactState \in [Jobs -> ArtifactStates]
    /\ needsCollection \in [Jobs -> BOOLEAN]
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

NoDuplicateActiveOwner ==
    \A j \in Jobs :
        /\ (lcState[j] \in {"claimed", "running", "syncing"} => claimedBy[j] = "worker")
        /\ (lcState[j] \in {"queued", "completed", "failed"} => claimedBy[j] = "none")

RetryCountMatchesRequeues ==
    \A j \in Jobs :
        retryCount[j] = requeueCount[j]

TerminalJobsNeverResurrect ==
    \A j \in Jobs :
        [](
            lcState[j] \in {"completed", "failed"} =>
                [](
                    /\ lcState[j] \in {"completed", "failed"}
                    /\ (lcState[j] = "completed" => rqState[j] = "rq_done")
                    /\ (lcState[j] = "failed" => rqState[j] = "rq_failed")
                    /\ claimedBy[j] = "none"
                )
        )

ArtifactTerminalStateMatchesLifecycle ==
    /\ \A j \in Jobs :
        [](
            /\ (artifactState[j] = "success" => lcState[j] # "failed")
            /\ (artifactState[j] = "fail" => lcState[j] # "completed")
        )
    /\ \A j \in Jobs :
        [](
            /\ (artifactState[j] = "success" => <>(lcState[j] = "completed"))
            /\ (artifactState[j] = "fail" => <>(lcState[j] = "failed"))
        )

ResumeReconciliationIsComplete ==
    \A j \in Jobs :
        [](needsCollection[j] => <> ~needsCollection[j])

HealthyWorkerCannotBeOrphaned ==
    \A j \in Jobs :
        [](
            workerAlive[j] /\ hbFresh[j] /\ lcState[j] \in {"claimed", "running", "syncing"}
                => lcState[j] # "orphaned"
        )

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

AdvanceRunningOrClaimed(j) ==
    EnterSyncing(j) \/ ExplicitFail(j)

AdvanceSyncing(j) ==
    PublishSuccessArtifacts(j)
        \/ CompleteJob(j)
        \/ ExplicitFail(j)
        \/ ResumeReconcileSuccess(j)
        \/ ResumeReconcileFailure(j)

TimeoutRecover(j) ==
    TimeoutRecoverToCompletedFromArtifact(j)
        \/ TimeoutRecoverToFailedFromArtifact(j)
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
