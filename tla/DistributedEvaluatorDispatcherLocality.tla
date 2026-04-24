---- MODULE DistributedEvaluatorDispatcherLocality ----
EXTENDS TLC, Naturals

\* Bounded model for dispatcher-owned evaluator routing with evaluator-local
\* advisory warmup and a toggle for verify flows that either require build-lineage
\* locality (`EnforceLocalVerify = TRUE`) or may rebalance after build success
\* and rebuild locally on cache miss (`EnforceLocalVerify = FALSE`).
\*
\* Scope:
\* - one shared lineage
\* - two trial owners with verify requests
\* - two evaluators, one of which may join later
\* - optional evaluator-local warmup dispatch before first verify demand
\* - queued-build rebalance before build start
\* - evaluator death causing lineage generation advance
\* - stale-attempt fencing on late verify publication
\*
\* Non-goals:
\* - full Redis schema or cloud bring-up
\* - exact RQ queue behavior
\* - multi-lineage load balancing

CONSTANTS EnforceLocalVerify, FenceAttemptPublishes, SuppressWarmupOnDemand

Evaluators == {"eval1", "eval2"}
Owners == {"ownerA", "ownerB"}
VerifyStates == {"pending", "blocked", "ready", "running", "succeeded"}
BuildStates == {"idle", "queued", "running", "succeeded"}
AttemptNos == 1..2
Generations == 1..2
NoEval == "<<no-eval>>"
NoOwner == "<<no-owner>>"
NoAttempt ==
    [owner |-> NoOwner, num |-> 0, eval |-> NoEval, gen |-> 0]

VerifyAttempts ==
    {
        [owner |-> owner, num |-> num, eval |-> eval, gen |-> gen] :
            owner \in Owners,
            num \in AttemptNos,
            eval \in Evaluators,
            gen \in Generations
    }

VARIABLES alive,
          joined2,
          dead1,
          lineageOwner,
          generation,
          warmupEnabled,
          warmupQueued,
          requiredBuildDemand,
          warmupDispatchedWhileDemanded,
          buildState,
          builtOn,
          verifyState,
          attemptNo,
          authAttempt,
          terminalAttempt,
          attemptsInWorld,
          lastVerifyOwner

vars ==
    <<alive,
      joined2,
      dead1,
      lineageOwner,
      generation,
      warmupEnabled,
      warmupQueued,
      requiredBuildDemand,
      warmupDispatchedWhileDemanded,
      buildState,
      builtOn,
      verifyState,
      attemptNo,
      authAttempt,
      terminalAttempt,
      attemptsInWorld,
      lastVerifyOwner>>

OtherOwner(owner) ==
    IF owner = "ownerA" THEN "ownerB" ELSE "ownerA"

Ready(owner) ==
    verifyState[owner] = "ready"

Served(owner) ==
    verifyState[owner] = "running" \/ verifyState[owner] = "succeeded"

Outstanding(owner) ==
    verifyState[owner] \in {"blocked", "ready", "running"}

NextReadyOwner ==
    IF Ready("ownerA") /\ Ready("ownerB")
    THEN IF lastVerifyOwner = "ownerA" THEN "ownerB" ELSE "ownerA"
    ELSE IF Ready("ownerA")
         THEN "ownerA"
         ELSE IF Ready("ownerB")
              THEN "ownerB"
              ELSE NoOwner

CurrentGenerationReadyForVerify ==
    /\ buildState = "succeeded"
    /\ IF EnforceLocalVerify
          THEN /\ lineageOwner \in alive
               /\ lineageOwner # NoEval
               /\ builtOn[generation] = lineageOwner
          ELSE builtOn[generation] # NoEval

LocalVerifyRecovery ==
    buildState # "idle"
    \/ (\E owner \in Owners :
            Outstanding(owner) /\ terminalAttempt[owner] = NoAttempt)

RebalancedVerifyRecovery ==
    buildState \in {"queued", "running"}
    \/ (\E owner \in Owners :
            verifyState[owner] = "running"
            /\ terminalAttempt[owner] = NoAttempt)

NeedsRecovery ==
    IF EnforceLocalVerify
    THEN LocalVerifyRecovery
    ELSE RebalancedVerifyRecovery

Init ==
    /\ alive = {"eval1"}
    /\ joined2 = FALSE
    /\ dead1 = FALSE
    /\ lineageOwner = "eval1"
    /\ warmupEnabled = TRUE
    /\ warmupQueued = FALSE
    /\ requiredBuildDemand = FALSE
    /\ warmupDispatchedWhileDemanded = FALSE
    /\ generation = 1
    /\ buildState = "idle"
    /\ builtOn = [gen \in Generations |-> NoEval]
    /\ verifyState = [owner \in Owners |-> "pending"]
    /\ attemptNo = [owner \in Owners |-> 0]
    /\ authAttempt = [owner \in Owners |-> NoAttempt]
    /\ terminalAttempt = [owner \in Owners |-> NoAttempt]
    /\ attemptsInWorld = {}
    /\ lastVerifyOwner = NoOwner

JoinEval2 ==
    /\ ~joined2
    /\ alive' = alive \cup {"eval2"}
    /\ joined2' = TRUE
    /\ UNCHANGED
        <<dead1,
          lineageOwner,
          generation,
          warmupEnabled,
          warmupQueued,
          requiredBuildDemand,
          warmupDispatchedWhileDemanded,
          buildState,
          builtOn,
          verifyState,
          attemptNo,
          authAttempt,
          terminalAttempt,
          attemptsInWorld,
          lastVerifyOwner>>

WarmupDispatch ==
    /\ warmupEnabled
    /\ alive # {}
    /\ ~warmupQueued
    /\ IF SuppressWarmupOnDemand THEN ~requiredBuildDemand ELSE TRUE
    /\ warmupQueued' = TRUE
    /\ warmupDispatchedWhileDemanded' =
         (warmupDispatchedWhileDemanded \/ requiredBuildDemand)
    /\ UNCHANGED
        <<alive,
          joined2,
          dead1,
          lineageOwner,
          generation,
          warmupEnabled,
          requiredBuildDemand,
          buildState,
          builtOn,
          verifyState,
          attemptNo,
          authAttempt,
          terminalAttempt,
          attemptsInWorld,
          lastVerifyOwner>>

WarmupDrains ==
    /\ warmupQueued
    /\ warmupQueued' = FALSE
    /\ UNCHANGED
        <<alive,
          joined2,
          dead1,
          lineageOwner,
          generation,
          warmupEnabled,
          requiredBuildDemand,
          warmupDispatchedWhileDemanded,
          buildState,
          builtOn,
          verifyState,
          attemptNo,
          authAttempt,
          terminalAttempt,
          attemptsInWorld,
          lastVerifyOwner>>

DemandBuildArrives(owner) ==
    /\ owner \in Owners
    /\ verifyState[owner] = "pending"
    /\ terminalAttempt[owner] = NoAttempt
    /\ IF CurrentGenerationReadyForVerify
          THEN /\ verifyState' = [verifyState EXCEPT ![owner] = "ready"]
               /\ buildState' = buildState
               /\ requiredBuildDemand' = requiredBuildDemand
          ELSE /\ verifyState' = [verifyState EXCEPT ![owner] = "blocked"]
               /\ buildState' = IF buildState = "idle" THEN "queued" ELSE buildState
               /\ requiredBuildDemand' = TRUE
    /\ UNCHANGED
        <<alive,
          joined2,
          dead1,
          lineageOwner,
          generation,
          warmupEnabled,
          warmupQueued,
          warmupDispatchedWhileDemanded,
          builtOn,
          attemptNo,
          authAttempt,
          terminalAttempt,
          attemptsInWorld,
          lastVerifyOwner>>

RebalanceQueuedBuildToEval2 ==
    /\ joined2
    /\ "eval2" \in alive
    /\ ~dead1
    /\ lineageOwner = "eval1"
    /\ buildState = "queued"
    /\ lineageOwner' = "eval2"
    /\ UNCHANGED
        <<alive,
          joined2,
          dead1,
          generation,
          warmupEnabled,
          warmupQueued,
          requiredBuildDemand,
          warmupDispatchedWhileDemanded,
          buildState,
          builtOn,
          verifyState,
          attemptNo,
          authAttempt,
          terminalAttempt,
          attemptsInWorld,
          lastVerifyOwner>>

AssignUnownedLineageToEval2 ==
    /\ lineageOwner = NoEval
    /\ "eval2" \in alive
    /\ buildState = "queued"
    /\ lineageOwner' = "eval2"
    /\ UNCHANGED
        <<alive,
          joined2,
          dead1,
          generation,
          warmupEnabled,
          warmupQueued,
          requiredBuildDemand,
          warmupDispatchedWhileDemanded,
          buildState,
          builtOn,
          verifyState,
          attemptNo,
          authAttempt,
          terminalAttempt,
          attemptsInWorld,
          lastVerifyOwner>>

StartBuild ==
    /\ buildState = "queued"
    /\ lineageOwner \in alive
    /\ lineageOwner # NoEval
    /\ buildState' = "running"
    /\ UNCHANGED
        <<alive,
          joined2,
          dead1,
          lineageOwner,
          generation,
          warmupEnabled,
          warmupQueued,
          requiredBuildDemand,
          warmupDispatchedWhileDemanded,
          builtOn,
          verifyState,
          attemptNo,
          authAttempt,
          terminalAttempt,
          attemptsInWorld,
          lastVerifyOwner>>

FinishBuild ==
    /\ buildState = "running"
    /\ lineageOwner \in alive
    /\ lineageOwner # NoEval
    /\ buildState' = "succeeded"
    /\ builtOn' = [builtOn EXCEPT ![generation] = lineageOwner]
    /\ verifyState' =
        [owner \in Owners |->
            IF verifyState[owner] = "blocked" /\ terminalAttempt[owner] = NoAttempt
            THEN "ready"
            ELSE verifyState[owner]
        ]
    /\ requiredBuildDemand' = FALSE
    /\ UNCHANGED
        <<alive,
          joined2,
          dead1,
          lineageOwner,
          generation,
          warmupEnabled,
          warmupQueued,
          warmupDispatchedWhileDemanded,
          attemptNo,
          authAttempt,
          terminalAttempt,
          attemptsInWorld,
          lastVerifyOwner>>

Eval1Dies ==
    /\ ~dead1
    /\ "eval1" \in alive
    /\ alive' = alive \ {"eval1"}
    /\ dead1' = TRUE
    /\ IF lineageOwner = "eval1" /\ NeedsRecovery
          THEN /\ generation' =
                    IF EnforceLocalVerify \/ buildState # "succeeded"
                    THEN 2
                    ELSE generation
               /\ lineageOwner' =
                    IF "eval2" \in alive THEN "eval2" ELSE NoEval
               /\ buildState' =
                    IF EnforceLocalVerify \/ buildState # "succeeded"
                    THEN "queued"
                    ELSE buildState
               /\ verifyState' =
                    [owner \in Owners |->
                        IF terminalAttempt[owner] # NoAttempt
                        THEN verifyState[owner]
                        ELSE IF EnforceLocalVerify \/ buildState # "succeeded"
                             THEN IF Outstanding(owner)
                                  THEN "blocked"
                                  ELSE verifyState[owner]
                             ELSE IF verifyState[owner] = "running"
                                  THEN "ready"
                                  ELSE verifyState[owner]
                    ]
               /\ authAttempt' =
                    [owner \in Owners |->
                        IF terminalAttempt[owner] # NoAttempt
                        THEN authAttempt[owner]
                        ELSE IF EnforceLocalVerify \/ buildState # "succeeded"
                             THEN IF Outstanding(owner)
                                  THEN NoAttempt
                                  ELSE authAttempt[owner]
                             ELSE IF verifyState[owner] = "running"
                                  THEN NoAttempt
                                  ELSE authAttempt[owner]
                    ]
               /\ requiredBuildDemand' =
                    IF EnforceLocalVerify \/ buildState # "succeeded"
                    THEN \E owner \in Owners :
                             Outstanding(owner)
                             /\ terminalAttempt[owner] = NoAttempt
                    ELSE FALSE
          ELSE /\ generation' = generation
               /\ lineageOwner' =
                    IF lineageOwner = "eval1"
                    THEN IF "eval2" \in alive THEN "eval2" ELSE NoEval
                    ELSE lineageOwner
               /\ buildState' =
                    IF EnforceLocalVerify /\ lineageOwner = "eval1"
                    THEN "idle"
                    ELSE buildState
               /\ verifyState' = verifyState
               /\ authAttempt' = authAttempt
               /\ requiredBuildDemand' = requiredBuildDemand
    /\ UNCHANGED
        <<joined2,
          warmupEnabled,
          warmupQueued,
          warmupDispatchedWhileDemanded,
          builtOn,
          attemptNo,
          terminalAttempt,
          attemptsInWorld,
          lastVerifyOwner>>

DispatchVerify(owner, eval) ==
    /\ owner = NextReadyOwner
    /\ owner \in Owners
    /\ eval \in alive
    /\ CurrentGenerationReadyForVerify
    /\ verifyState[owner] = "ready"
    /\ terminalAttempt[owner] = NoAttempt
    /\ attemptNo[owner] < 2
    /\ IF EnforceLocalVerify THEN eval = lineageOwner ELSE TRUE
    /\ LET att ==
            [owner |-> owner,
             num |-> attemptNo[owner] + 1,
             eval |-> eval,
             gen |-> generation]
       IN /\ att \in VerifyAttempts
          /\ verifyState' = [verifyState EXCEPT ![owner] = "running"]
          /\ attemptNo' = [attemptNo EXCEPT ![owner] = @ + 1]
          /\ authAttempt' = [authAttempt EXCEPT ![owner] = att]
          /\ attemptsInWorld' = attemptsInWorld \cup {att}
          /\ lastVerifyOwner' = owner
    /\ UNCHANGED
        <<alive,
          joined2,
          dead1,
          lineageOwner,
          generation,
          warmupEnabled,
          warmupQueued,
          requiredBuildDemand,
          warmupDispatchedWhileDemanded,
          buildState,
          builtOn,
          terminalAttempt>>

PublishVerify(owner, att) ==
    /\ owner \in Owners
    /\ att \in attemptsInWorld
    /\ att.owner = owner
    /\ terminalAttempt[owner] = NoAttempt
    /\ IF FenceAttemptPublishes THEN att = authAttempt[owner] ELSE TRUE
    /\ verifyState' = [verifyState EXCEPT ![owner] = "succeeded"]
    /\ terminalAttempt' = [terminalAttempt EXCEPT ![owner] = att]
    /\ UNCHANGED
        <<alive,
          joined2,
          dead1,
          lineageOwner,
          generation,
          warmupEnabled,
          warmupQueued,
          requiredBuildDemand,
          warmupDispatchedWhileDemanded,
          buildState,
          builtOn,
          attemptNo,
          authAttempt,
          attemptsInWorld,
          lastVerifyOwner>>

Stutter ==
    UNCHANGED vars

Next ==
    \/ JoinEval2
    \/ WarmupDispatch
    \/ WarmupDrains
    \/ \E owner \in Owners : DemandBuildArrives(owner)
    \/ RebalanceQueuedBuildToEval2
    \/ AssignUnownedLineageToEval2
    \/ StartBuild
    \/ FinishBuild
    \/ Eval1Dies
    \/ \E owner \in Owners, eval \in Evaluators : DispatchVerify(owner, eval)
    \/ \E owner \in Owners, att \in VerifyAttempts : PublishVerify(owner, att)
    \/ Stutter

TypeInvariant ==
    /\ alive \subseteq Evaluators
    /\ joined2 \in BOOLEAN
    /\ dead1 \in BOOLEAN
    /\ lineageOwner \in Evaluators \cup {NoEval}
    /\ generation \in Generations
    /\ warmupEnabled \in BOOLEAN
    /\ warmupQueued \in BOOLEAN
    /\ requiredBuildDemand \in BOOLEAN
    /\ warmupDispatchedWhileDemanded \in BOOLEAN
    /\ buildState \in BuildStates
    /\ builtOn \in [Generations -> Evaluators \cup {NoEval}]
    /\ verifyState \in [Owners -> VerifyStates]
    /\ attemptNo \in [Owners -> 0..2]
    /\ authAttempt \in [Owners -> VerifyAttempts \cup {NoAttempt}]
    /\ terminalAttempt \in [Owners -> VerifyAttempts \cup {NoAttempt}]
    /\ attemptsInWorld \subseteq VerifyAttempts
    /\ lastVerifyOwner \in Owners \cup {NoOwner}

ReadyRequiresBuiltGeneration ==
    \A owner \in Owners :
        Ready(owner) =>
            CurrentGenerationReadyForVerify

RunningVerifyUsesCurrentGeneration ==
    \A owner \in Owners :
        verifyState[owner] = "running" =>
            /\ authAttempt[owner] # NoAttempt
            /\ authAttempt[owner].gen = generation
            /\ IF EnforceLocalVerify
                  THEN /\ authAttempt[owner].eval = lineageOwner
                       /\ builtOn[generation] = lineageOwner
                  ELSE builtOn[generation] # NoEval

TerminalAttemptMatchesAuthoritativeAssignment ==
    \A owner \in Owners :
        terminalAttempt[owner] = NoAttempt
        \/ terminalAttempt[owner] = authAttempt[owner]

AcceptedTerminalUsesBuiltGeneration ==
    \A owner \in Owners :
        terminalAttempt[owner] = NoAttempt
        \/ IF EnforceLocalVerify
              THEN builtOn[terminalAttempt[owner].gen] = terminalAttempt[owner].eval
              ELSE builtOn[terminalAttempt[owner].gen] # NoEval

NoWarmupDispatchWhileDemanded ==
    ~warmupDispatchedWhileDemanded

VerifyOwnerNoStarvation ==
    \A owner \in Owners :
        Ready(owner) ~> Served(owner)

EventuallyAllTerminal ==
    <>(\A owner \in Owners : terminalAttempt[owner] # NoAttempt)

Spec ==
    Init /\ [][Next]_vars

HealthySpec ==
    Spec
        /\ WF_vars(JoinEval2)
        /\ \A demandOwner \in Owners :
            WF_vars(DemandBuildArrives(demandOwner))
        /\ WF_vars(RebalanceQueuedBuildToEval2)
        /\ WF_vars(AssignUnownedLineageToEval2)
        /\ WF_vars(StartBuild)
        /\ WF_vars(FinishBuild)
        /\ \A verifyOwner \in Owners, eval \in Evaluators :
            WF_vars(DispatchVerify(verifyOwner, eval))
        /\ \A publishOwner \in Owners :
            WF_vars(PublishVerify(publishOwner, authAttempt[publishOwner]))

=============================================================================
