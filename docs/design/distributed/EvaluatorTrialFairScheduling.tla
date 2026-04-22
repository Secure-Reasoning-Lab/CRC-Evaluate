---- MODULE EvaluatorTrialFairScheduling ----
EXTENDS Naturals, Sequences, FiniteSets, TLC

\* Bounded TLA+ model for the current evaluator fairness revision.
\* Scope:
\* - local fair selection over queued build/verify work
\* - build-before-verify dependency gating
\* - no silent job loss on claim failure / pre-start retry paths
\* - owner no-starvation in a small canonical scenario
\* Non-goal:
\* - a distributed global turn ledger across multiple evaluators

QueueClasses == {"build", "verify"}
NoOwner == "<<no-owner>>"
NoClass == "<<no-class>>"
NoJob == "<<no-job>>"
Owners == {"ownerA", "ownerB"}
BuildJobs == {"buildA", "buildB"}
VerifyJobs == {"verifyA", "verifyB"}
Jobs == BuildJobs \cup VerifyJobs
InitialBuildQueue == <<"buildA", "buildB">>
InitialVerifyQueue == <<"verifyA", "verifyB">>
InitialFailureBudget ==
    [job \in Jobs |->
        IF job = "buildA" \/ job = "verifyA"
        THEN 1
        ELSE 0
    ]

SeqToSet(seq) == {seq[i] : i \in 1..Len(seq)}

NoDuplicates(seq) ==
    \A i, j \in 1..Len(seq) : i # j => seq[i] # seq[j]

IsQueue(seq, jobSet) ==
    /\ seq \in Seq(jobSet)
    /\ NoDuplicates(seq)
    /\ SeqToSet(seq) = jobSet

VARIABLES queued,
          started,
          done,
          nextClass,
          lastOwner,
          failureBudget

vars == <<queued, started, done, nextClass, lastOwner, failureBudget>>

ASSUME /\ NoOwner \notin Owners
       /\ NoClass \notin QueueClasses
       /\ NoJob \notin Jobs
       /\ IsQueue(InitialBuildQueue, BuildJobs)
       /\ IsQueue(InitialVerifyQueue, VerifyJobs)
       /\ InitialFailureBudget \in [Jobs -> 0..1]

JobOwner(job) ==
    IF job = "buildA" \/ job = "verifyA"
    THEN "ownerA"
    ELSE "ownerB"

VerifyDependsOn(job) ==
    IF job = "verifyA"
    THEN "buildA"
    ELSE "buildB"

JobClass(job) ==
    IF job \in BuildJobs THEN "build" ELSE "verify"

ToggleClass(class) ==
    IF class = "build" THEN "verify" ELSE "build"

RECURSIVE FilterReadyVerify(_)
FilterReadyVerify(seq) ==
    IF Len(seq) = 0
    THEN <<>>
    ELSE IF VerifyDependsOn(Head(seq)) \in done
         THEN <<Head(seq)>> \o FilterReadyVerify(Tail(seq))
         ELSE FilterReadyVerify(Tail(seq))

ReadySeq(class) ==
    IF class = "build"
    THEN queued["build"]
    ELSE FilterReadyVerify(queued["verify"])

RECURSIVE UniqueOwners(_, _)
UniqueOwners(seq, seen) ==
    IF Len(seq) = 0
    THEN <<>>
    ELSE LET owner == JobOwner(Head(seq))
         IN IF owner \in seen
            THEN UniqueOwners(Tail(seq), seen)
            ELSE <<owner>> \o UniqueOwners(Tail(seq), seen \cup {owner})

OwnerSeq(class) ==
    UniqueOwners(ReadySeq(class), {})

RECURSIVE IndexOf(_, _)
IndexOf(seq, value) ==
    IF Len(seq) = 0
    THEN 0
    ELSE IF Head(seq) = value
         THEN 1
         ELSE LET tailIndex == IndexOf(Tail(seq), value)
              IN IF tailIndex = 0 THEN 0 ELSE 1 + tailIndex

NextOwner(class) ==
    LET owners == OwnerSeq(class)
    IN IF Len(owners) = 0
       THEN NoOwner
       ELSE IF lastOwner[class] \notin SeqToSet(owners) \/ Len(owners) = 1
            THEN owners[1]
            ELSE LET ownerIndex == IndexOf(owners, lastOwner[class])
                 IN owners[
                     IF ownerIndex = Len(owners)
                     THEN 1
                     ELSE ownerIndex + 1
                 ]

RECURSIVE FirstJobForOwner(_, _)
FirstJobForOwner(seq, owner) ==
    IF Len(seq) = 0
    THEN NoJob
    ELSE IF JobOwner(Head(seq)) = owner
         THEN Head(seq)
         ELSE FirstJobForOwner(Tail(seq), owner)

ChosenJob(class) ==
    LET owner == NextOwner(class)
    IN IF owner = NoOwner
       THEN NoJob
       ELSE FirstJobForOwner(ReadySeq(class), owner)

ClassReady(class) ==
    Len(ReadySeq(class)) > 0

ChosenClass ==
    IF ClassReady("build") /\ ClassReady("verify")
    THEN nextClass
    ELSE IF ClassReady("build")
         THEN "build"
         ELSE IF ClassReady("verify")
              THEN "verify"
              ELSE NoClass

RECURSIVE RemoveFirst(_, _)
RemoveFirst(seq, value) ==
    IF Len(seq) = 0
    THEN <<>>
    ELSE IF Head(seq) = value
         THEN Tail(seq)
         ELSE <<Head(seq)>> \o RemoveFirst(Tail(seq), value)

QueuedJobs ==
    SeqToSet(queued["build"]) \cup SeqToSet(queued["verify"])

OwnerReady(class, owner) ==
    \E job \in SeqToSet(ReadySeq(class)) : JobOwner(job) = owner

OwnerServed(class, owner) ==
    \E job \in started \cup done :
        /\ JobClass(job) = class
        /\ JobOwner(job) = owner

Init ==
    /\ queued = [class \in QueueClasses |-> IF class = "build" THEN InitialBuildQueue ELSE InitialVerifyQueue]
    /\ started = {}
    /\ done = {}
    /\ nextClass = "verify"
    /\ lastOwner = [class \in QueueClasses |-> NoOwner]
    /\ failureBudget = InitialFailureBudget

TypeOK ==
    /\ queued["build"] \in Seq(BuildJobs)
    /\ queued["verify"] \in Seq(VerifyJobs)
    /\ NoDuplicates(queued["build"])
    /\ NoDuplicates(queued["verify"])
    /\ started \subseteq Jobs
    /\ done \subseteq Jobs
    /\ nextClass \in QueueClasses
    /\ lastOwner \in [QueueClasses -> Owners \cup {NoOwner}]
    /\ failureBudget \in [Jobs -> Nat]

NoSilentLoss ==
    /\ SeqToSet(queued["build"]) \cap SeqToSet(queued["verify"]) = {}
    /\ SeqToSet(queued["build"]) \cap started = {}
    /\ SeqToSet(queued["build"]) \cap done = {}
    /\ SeqToSet(queued["verify"]) \cap started = {}
    /\ SeqToSet(queued["verify"]) \cap done = {}
    /\ started \cap done = {}
    /\ QueuedJobs \cup started \cup done = Jobs

VerifyNeverStartsEarly ==
    \A job \in (started \cup done) \cap VerifyJobs :
        VerifyDependsOn(job) \in done

Serve ==
    LET class == ChosenClass
    IN /\ class \in QueueClasses
       /\ LET job == ChosenJob(class)
          IN /\ job \in Jobs
             /\ failureBudget[job] = 0
             /\ queued' = [queued EXCEPT ![class] = RemoveFirst(@, job)]
             /\ started' = started \cup {job}
             /\ done' = done
             /\ nextClass' =
                    IF ClassReady("build") /\ ClassReady("verify")
                    THEN ToggleClass(class)
                    ELSE nextClass
             /\ lastOwner' = [lastOwner EXCEPT ![class] = JobOwner(job)]
             /\ failureBudget' = failureBudget

ClaimFail ==
    LET class == ChosenClass
    IN /\ class \in QueueClasses
       /\ LET job == ChosenJob(class)
          IN /\ job \in Jobs
             /\ failureBudget[job] > 0
             /\ queued' = queued
             /\ started' = started
             /\ done' = done
             /\ nextClass' = nextClass
             /\ lastOwner' = lastOwner
             /\ failureBudget' = [failureBudget EXCEPT ![job] = @ - 1]

Finish(job) ==
    /\ job \in started
    /\ queued' = queued
    /\ started' = started \ {job}
    /\ done' = done \cup {job}
    /\ nextClass' = nextClass
    /\ lastOwner' = lastOwner
    /\ failureBudget' = failureBudget

Next ==
    \/ Serve
    \/ ClaimFail
    \/ \E job \in started : Finish(job)

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(Serve)
    /\ WF_vars(ClaimFail)
    /\ \A job \in Jobs : WF_vars(Finish(job))

BuildOwnerNoStarvation ==
    \A owner \in Owners :
        OwnerReady("build", owner) ~> OwnerServed("build", owner)

VerifyOwnerNoStarvation ==
    \A owner \in Owners :
        OwnerReady("verify", owner) ~> OwnerServed("verify", owner)

EventuallyDrains ==
    <>(QueuedJobs = {} /\ started = {})

====
