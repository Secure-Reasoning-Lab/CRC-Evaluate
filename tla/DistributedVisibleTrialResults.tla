---- MODULE DistributedVisibleTrialResults ----
EXTENDS Naturals, FiniteSets

\* Code-correspondent model for final report projection:
\* - multiple physical RQ jobs may exist for one logical trial_key
\* - workers/orchestrator still publish one canonical `.success` / `.fail`
\*   marker in the logical trial directory
\* - final user-visible reporting must count logical trials, not physical jobs

CONSTANT SecondarySharesLogicalTrial, ProjectionUsesCanonicalMarker

Jobs == {"job_a", "job_b"}
TrialKeys == {"trial_a", "trial_b"}
JobStates == {"pending", "success", "fail"}
Markers == {"none", "success", "fail"}

LogicalTrialKey(j) ==
    IF j = "job_a" THEN
        "trial_a"
    ELSE IF SecondarySharesLogicalTrial THEN
        "trial_a"
    ELSE
        "trial_b"

VARIABLES jobState, marker

vars == <<jobState, marker>>

Init ==
    /\ jobState = [j \in Jobs |-> "pending"]
    /\ marker = [k \in TrialKeys |-> "none"]

FinishSuccess(j) ==
    LET k == LogicalTrialKey(j) IN
    /\ jobState[j] = "pending"
    /\ jobState' = [jobState EXCEPT ![j] = "success"]
    /\ marker' = [marker EXCEPT ![k] = "success"]

FinishFail(j) ==
    LET k == LogicalTrialKey(j) IN
    /\ jobState[j] = "pending"
    /\ jobState' = [jobState EXCEPT ![j] = "fail"]
    /\ marker' = [marker EXCEPT ![k] = "fail"]

Next ==
    \E j \in Jobs :
        \/ FinishSuccess(j)
        \/ FinishFail(j)
    \/ UNCHANGED vars

PhysicalTerminalCount(k) ==
    Cardinality(
        {j \in Jobs : LogicalTrialKey(j) = k /\ jobState[j] \in {"success", "fail"}}
    )

VisibleTrialCount(k) ==
    IF ProjectionUsesCanonicalMarker THEN
        IF marker[k] = "none" THEN PhysicalTerminalCount(k) ELSE 1
    ELSE
        PhysicalTerminalCount(k)

VisibleSuccessCount(k) ==
    IF ProjectionUsesCanonicalMarker THEN
        IF marker[k] = "success" THEN 1 ELSE 0
    ELSE
        Cardinality(
            {j \in Jobs : LogicalTrialKey(j) = k /\ jobState[j] = "success"}
        )

VisibleFailureCount(k) ==
    IF ProjectionUsesCanonicalMarker THEN
        IF marker[k] = "fail" THEN 1 ELSE 0
    ELSE
        Cardinality(
            {j \in Jobs : LogicalTrialKey(j) = k /\ jobState[j] = "fail"}
        )

TypeInvariant ==
    /\ jobState \in [Jobs -> JobStates]
    /\ marker \in [TrialKeys -> Markers]

AtMostOneVisibleOutcomePerTrial ==
    \A k \in TrialKeys : VisibleTrialCount(k) <= 1

VisibleOutcomeMatchesCanonicalMarker ==
    \A k \in TrialKeys :
        /\ marker[k] = "success" => /\ VisibleSuccessCount(k) = 1 /\ VisibleFailureCount(k) = 0
        /\ marker[k] = "fail" => /\ VisibleSuccessCount(k) = 0 /\ VisibleFailureCount(k) = 1

Spec ==
    Init /\ [][Next]_vars

=============================================================================
