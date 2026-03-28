---- MODULE DistributedRetryExclusivity ----
EXTENDS TLC, FiniteSets, Naturals

\* Code correspondence:
\* - one logical trial_key may be retried after stale-worker recovery
\* - ownership/fencing determines which attempt is still allowed to progress
\* - after supersession there must be at most one authoritative live attempt

CONSTANT FencingEnabled

Workers == {"worker_1", "worker_2"}
Stages == {"original_running", "replacement_running", "done"}

VARIABLES activeAttempts, stage

vars == <<activeAttempts, stage>>

Init ==
    /\ activeAttempts = {"worker_1"}
    /\ stage = "original_running"

SupersedeAndRetry ==
    /\ stage = "original_running"
    /\ activeAttempts' =
        IF FencingEnabled
            THEN {"worker_2"}
            ELSE {"worker_1", "worker_2"}
    /\ stage' = "replacement_running"

ReplacementCompletes ==
    /\ stage = "replacement_running"
    /\ "worker_2" \in activeAttempts
    /\ activeAttempts' = {}
    /\ stage' = "done"

Stutter ==
    UNCHANGED vars

Next ==
    \/ SupersedeAndRetry
    \/ ReplacementCompletes
    \/ Stutter

TypeInvariant ==
    /\ activeAttempts \subseteq Workers
    /\ stage \in Stages

AtMostOneLiveAttemptPerTrialKeyAcrossRetries ==
    Cardinality(activeAttempts) <= 1

EventuallyReplacementBecomesOnlyLiveAttempt ==
    <>(stage = "replacement_running" /\ activeAttempts = {"worker_2"})

Spec ==
    Init /\ [][Next]_vars

HealthySpec ==
    Spec
        /\ WF_vars(SupersedeAndRetry)

=============================================================================
