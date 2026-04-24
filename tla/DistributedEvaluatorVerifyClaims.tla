---- MODULE DistributedEvaluatorVerifyClaims ----
EXTENDS TLC, Naturals

\* Bounded model for owner-fair logical verify claims with evaluator-local
\* build/verify DAG materialization.
\*
\* Scope:
\* - two trial owners, one logical verify request each
\* - two evaluators
\* - each claimed request needs one local build before verify may run
\* - evaluator-local warmup may run only while no required local build demand exists
\* - claim expiry allows reclaim
\* - stale local verify publication must be fenced to the current live claim
\*
\* Non-goals:
\* - exact Redis/RQ schema
\* - build artifact deduplication across evaluators
\* - queue-depth or CPU allocation details inside one evaluator

CONSTANTS EnforceFairClaims, FenceExpiredPublishes, SuppressWarmupOnDemand

Evaluators == {"eval1", "eval2"}
Owners == {"ownerA", "ownerB"}
NoEval == "<<no-eval>>"
NoOwner == "<<no-owner>>"
ReqStates == {"pending", "claimed", "build_queued", "build_ready", "verify_running", "published"}

VARIABLES alive,
          reqState,
          claimedBy,
          claimLive,
          localEval,
          buildReady,
          runningEval,
          publishedBy,
          lastClaimOwner,
          warmupQueued,
          warmupDispatchedWhileDemanded,
          fairnessViolated,
          stalePublishAccepted

vars ==
    <<alive,
      reqState,
      claimedBy,
      claimLive,
      localEval,
      buildReady,
      runningEval,
      publishedBy,
      lastClaimOwner,
      warmupQueued,
      warmupDispatchedWhileDemanded,
      fairnessViolated,
      stalePublishAccepted>>

RequiredBuildDemand(eval) ==
    \E owner \in Owners :
        localEval[owner] = eval
        /\ reqState[owner] \in {"claimed", "build_queued"}

ReadyOwners ==
    {owner \in Owners : reqState[owner] = "pending"}

ExpectedNextOwner ==
    IF ReadyOwners = {"ownerA", "ownerB"}
    THEN IF lastClaimOwner = "ownerA" THEN "ownerB" ELSE "ownerA"
    ELSE IF "ownerA" \in ReadyOwners
         THEN "ownerA"
         ELSE IF "ownerB" \in ReadyOwners
              THEN "ownerB"
              ELSE NoOwner

Init ==
    /\ alive = Evaluators
    /\ reqState = [owner \in Owners |-> "pending"]
    /\ claimedBy = [owner \in Owners |-> NoEval]
    /\ claimLive = [owner \in Owners |-> FALSE]
    /\ localEval = [owner \in Owners |-> NoEval]
    /\ buildReady = [owner \in Owners |-> FALSE]
    /\ runningEval = [owner \in Owners |-> NoEval]
    /\ publishedBy = [owner \in Owners |-> NoEval]
    /\ lastClaimOwner = NoOwner
    /\ warmupQueued = [eval \in Evaluators |-> FALSE]
    /\ warmupDispatchedWhileDemanded = FALSE
    /\ fairnessViolated = FALSE
    /\ stalePublishAccepted = FALSE

Claim(owner, eval) ==
    /\ owner \in ReadyOwners
    /\ eval \in alive
    /\ IF EnforceFairClaims /\ ReadyOwners = {"ownerA", "ownerB"}
          THEN owner = ExpectedNextOwner
          ELSE TRUE
    /\ reqState' = [reqState EXCEPT ![owner] = "claimed"]
    /\ claimedBy' = [claimedBy EXCEPT ![owner] = eval]
    /\ claimLive' = [claimLive EXCEPT ![owner] = TRUE]
    /\ localEval' = [localEval EXCEPT ![owner] = eval]
    /\ buildReady' = [buildReady EXCEPT ![owner] = FALSE]
    /\ runningEval' = [runningEval EXCEPT ![owner] = NoEval]
    /\ publishedBy' = publishedBy
    /\ lastClaimOwner' = owner
    /\ warmupQueued' = warmupQueued
    /\ warmupDispatchedWhileDemanded' = warmupDispatchedWhileDemanded
    /\ fairnessViolated' =
         IF ~EnforceFairClaims
            /\ ReadyOwners = {"ownerA", "ownerB"}
            /\ owner # ExpectedNextOwner
         THEN TRUE
         ELSE fairnessViolated
    /\ stalePublishAccepted' = stalePublishAccepted
    /\ UNCHANGED alive

QueueLocalBuild(owner) ==
    /\ owner \in Owners
    /\ reqState[owner] = "claimed"
    /\ reqState' = [reqState EXCEPT ![owner] = "build_queued"]
    /\ UNCHANGED
        <<alive,
          claimedBy,
          claimLive,
          localEval,
          buildReady,
          runningEval,
          publishedBy,
          lastClaimOwner,
          warmupQueued,
          warmupDispatchedWhileDemanded,
          fairnessViolated,
          stalePublishAccepted>>

BuildCompletes(owner) ==
    /\ owner \in Owners
    /\ reqState[owner] = "build_queued"
    /\ reqState' = [reqState EXCEPT ![owner] = "build_ready"]
    /\ buildReady' = [buildReady EXCEPT ![owner] = TRUE]
    /\ UNCHANGED
        <<alive,
          claimedBy,
          claimLive,
          localEval,
          runningEval,
          publishedBy,
          lastClaimOwner,
          warmupQueued,
          warmupDispatchedWhileDemanded,
          fairnessViolated,
          stalePublishAccepted>>

StartVerify(owner) ==
    /\ owner \in Owners
    /\ reqState[owner] = "build_ready"
    /\ buildReady[owner]
    /\ claimLive[owner]
    /\ reqState' = [reqState EXCEPT ![owner] = "verify_running"]
    /\ runningEval' = [runningEval EXCEPT ![owner] = claimedBy[owner]]
    /\ UNCHANGED
        <<alive,
          claimedBy,
          claimLive,
          localEval,
          buildReady,
          publishedBy,
          lastClaimOwner,
          warmupQueued,
          warmupDispatchedWhileDemanded,
          fairnessViolated,
          stalePublishAccepted>>

ExpireClaim(owner) ==
    /\ owner \in Owners
    /\ claimLive[owner]
    /\ claimLive' = [claimLive EXCEPT ![owner] = FALSE]
    /\ UNCHANGED
        <<alive,
          reqState,
          claimedBy,
          localEval,
          buildReady,
          runningEval,
          publishedBy,
          lastClaimOwner,
          warmupQueued,
          warmupDispatchedWhileDemanded,
          fairnessViolated,
          stalePublishAccepted>>

Reclaim(owner, eval) ==
    /\ owner \in Owners
    /\ reqState[owner] \in {"claimed", "build_queued", "build_ready", "verify_running"}
    /\ ~claimLive[owner]
    /\ eval \in alive
    /\ reqState' = [reqState EXCEPT ![owner] = "claimed"]
    /\ claimedBy' = [claimedBy EXCEPT ![owner] = eval]
    /\ claimLive' = [claimLive EXCEPT ![owner] = TRUE]
    /\ localEval' = [localEval EXCEPT ![owner] = eval]
    /\ buildReady' = [buildReady EXCEPT ![owner] = FALSE]
    /\ publishedBy' = publishedBy
    /\ lastClaimOwner' = owner
    /\ UNCHANGED
        <<alive,
          runningEval,
          warmupQueued,
          warmupDispatchedWhileDemanded,
          fairnessViolated,
          stalePublishAccepted>>

WarmupDispatch(eval) ==
    /\ eval \in alive
    /\ ~warmupQueued[eval]
    /\ IF SuppressWarmupOnDemand THEN ~RequiredBuildDemand(eval) ELSE TRUE
    /\ warmupQueued' = [warmupQueued EXCEPT ![eval] = TRUE]
    /\ warmupDispatchedWhileDemanded' =
         warmupDispatchedWhileDemanded \/ RequiredBuildDemand(eval)
    /\ UNCHANGED
        <<alive,
          reqState,
          claimedBy,
          claimLive,
          localEval,
          buildReady,
          runningEval,
          publishedBy,
          lastClaimOwner,
          fairnessViolated,
          stalePublishAccepted>>

WarmupDrains(eval) ==
    /\ eval \in Evaluators
    /\ warmupQueued[eval]
    /\ warmupQueued' = [warmupQueued EXCEPT ![eval] = FALSE]
    /\ UNCHANGED
        <<alive,
          reqState,
          claimedBy,
          claimLive,
          localEval,
          buildReady,
          runningEval,
          publishedBy,
          lastClaimOwner,
          warmupDispatchedWhileDemanded,
          fairnessViolated,
          stalePublishAccepted>>

Publish(owner, eval) ==
    /\ owner \in Owners
    /\ eval \in Evaluators
    /\ reqState[owner] = "verify_running"
    /\ runningEval[owner] = eval
    /\ IF FenceExpiredPublishes
          THEN claimLive[owner] /\ claimedBy[owner] = eval
          ELSE TRUE
    /\ reqState' = [reqState EXCEPT ![owner] = "published"]
    /\ claimLive' = [claimLive EXCEPT ![owner] = FALSE]
    /\ publishedBy' = [publishedBy EXCEPT ![owner] = eval]
    /\ stalePublishAccepted' =
         stalePublishAccepted
         \/ ~(claimLive[owner] /\ claimedBy[owner] = eval)
    /\ UNCHANGED
        <<alive,
          claimedBy,
          localEval,
          buildReady,
          runningEval,
          lastClaimOwner,
          warmupQueued,
          warmupDispatchedWhileDemanded,
          fairnessViolated>>

TypeInvariant ==
    /\ alive \subseteq Evaluators
    /\ reqState \in [Owners -> ReqStates]
    /\ claimedBy \in [Owners -> Evaluators \cup {NoEval}]
    /\ claimLive \in [Owners -> BOOLEAN]
    /\ localEval \in [Owners -> Evaluators \cup {NoEval}]
    /\ buildReady \in [Owners -> BOOLEAN]
    /\ runningEval \in [Owners -> Evaluators \cup {NoEval}]
    /\ publishedBy \in [Owners -> Evaluators \cup {NoEval}]
    /\ lastClaimOwner \in Owners \cup {NoOwner}
    /\ warmupQueued \in [Evaluators -> BOOLEAN]
    /\ warmupDispatchedWhileDemanded \in BOOLEAN
    /\ fairnessViolated \in BOOLEAN
    /\ stalePublishAccepted \in BOOLEAN

Next ==
    \/ \E claimOwner \in Owners, claimEval \in Evaluators :
           Claim(claimOwner, claimEval)
           \/ Reclaim(claimOwner, claimEval)
           \/ Publish(claimOwner, claimEval)
    \/ \E buildOwner \in Owners :
           QueueLocalBuild(buildOwner)
           \/ BuildCompletes(buildOwner)
           \/ StartVerify(buildOwner)
           \/ ExpireClaim(buildOwner)
    \/ \E warmupEval \in Evaluators :
           WarmupDispatch(warmupEval)
           \/ WarmupDrains(warmupEval)

Spec == Init /\ [][Next]_vars

HealthySpec == Spec

FairClaimRotation ==
    ~fairnessViolated

WarmupSuppressedOnDemand ==
    ~warmupDispatchedWhileDemanded

PublishedRequestsUseBuiltVariants ==
    \A owner \in Owners :
        reqState[owner] = "published" => buildReady[owner]

FencedPublication ==
    ~stalePublishAccepted

Invariant ==
    /\ TypeInvariant
    /\ FairClaimRotation
    /\ PublishedRequestsUseBuiltVariants
    /\ FencedPublication
    /\ IF SuppressWarmupOnDemand
          THEN WarmupSuppressedOnDemand
          ELSE TRUE

====
