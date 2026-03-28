---- MODULE DistributedTrialKeySnapshot ----
EXTENDS Naturals, FiniteSets

\* Code-correspondent model for the trial queue snapshot path:
\* - physical RQ jobs are distinct queue entries
\* - operator/continue-mode logic groups them by logical trial_key
\* - the current Python code stores one job per trial_key in a dict, so
\*   duplicate logical trials collapse to one visible row

CONSTANT SecondarySharesLogicalTrial

Jobs == {"job_a", "job_b"}
TrialKeys == {"trial_a", "trial_b"}
QueueStates == {"absent", "queued", "running", "finished", "failed"}

LogicalTrialKey(j) ==
    IF j = "job_a" THEN
        "trial_a"
    ELSE IF SecondarySharesLogicalTrial THEN
        "trial_a"
    ELSE
        "trial_b"

VARIABLES rqState

vars == <<rqState>>

Init ==
    rqState = [j \in Jobs |-> "absent"]

Enqueue(j) ==
    /\ rqState[j] = "absent"
    /\ rqState' = [rqState EXCEPT ![j] = "queued"]

Start(j) ==
    /\ rqState[j] = "queued"
    /\ rqState' = [rqState EXCEPT ![j] = "running"]

Finish(j) ==
    /\ rqState[j] \in {"queued", "running"}
    /\ rqState' = [rqState EXCEPT ![j] = "finished"]

Fail(j) ==
    /\ rqState[j] \in {"queued", "running"}
    /\ rqState' = [rqState EXCEPT ![j] = "failed"]

Next ==
    \E j \in Jobs :
        \/ Enqueue(j)
        \/ Start(j)
        \/ Finish(j)
        \/ Fail(j)

Active(j) ==
    rqState[j] \in {"queued", "running"}

ActiveCount(k) ==
    Cardinality({j \in Jobs : Active(j) /\ LogicalTrialKey(j) = k})

\* This matches get_existing_trials()/queue_monitor behavior: one dict slot per trial_key.
VisibleSnapshotCount(k) ==
    IF ActiveCount(k) = 0 THEN 0 ELSE 1

TypeInvariant ==
    rqState \in [Jobs -> QueueStates]

NoDuplicateLogicalTrialInFlight ==
    \A k \in TrialKeys :
        ActiveCount(k) <= 1

SnapshotCoversAllActivePhysicalJobs ==
    \A k \in TrialKeys :
        VisibleSnapshotCount(k) = ActiveCount(k)

Spec == Init /\ [][Next]_vars

=============================================================================
