---- MODULE DistributedRetryBudget ----
EXTENDS TLC, Naturals

\* Code correspondence:
\* - retry_count lives in distributed/job_lifecycle.py
\* - orphan recovery in distributed/job_monitor.py either requeues and increments
\*   retry_count or permanently fails when the retry budget is exhausted

CONSTANT MaxRetries, OffByOneBudgetBug

States == {"running", "queued", "failed"}
Phases == {"first_running", "after_first_recovery", "second_running", "done"}

VARIABLES lcState, retryCount, phase

vars == <<lcState, retryCount, phase>>

Init ==
    /\ MaxRetries \in Nat
    /\ MaxRetries > 0
    /\ lcState = "running"
    /\ retryCount = MaxRetries - 1
    /\ phase = "first_running"

Recover ==
    /\ phase \in {"first_running", "second_running"}
    /\ lcState = "running"
    /\ IF OffByOneBudgetBug
        THEN IF retryCount + 1 >= MaxRetries
                THEN /\ lcState' = "failed"
                     /\ retryCount' = retryCount
                     /\ phase' = "done"
                ELSE /\ lcState' = "queued"
                     /\ retryCount' = retryCount + 1
                     /\ phase' = "after_first_recovery"
        ELSE IF retryCount >= MaxRetries
                THEN /\ lcState' = "failed"
                     /\ retryCount' = retryCount
                     /\ phase' = "done"
                ELSE /\ lcState' = "queued"
                     /\ retryCount' = retryCount + 1
                     /\ phase' =
                            IF phase = "first_running"
                                THEN "after_first_recovery"
                                ELSE "done"

RestartRetriedAttempt ==
    /\ phase = "after_first_recovery"
    /\ lcState = "queued"
    /\ lcState' = "running"
    /\ retryCount' = retryCount
    /\ phase' = "second_running"

Stutter ==
    UNCHANGED vars

Next ==
    \/ Recover
    \/ RestartRetriedAttempt
    \/ Stutter

TypeInvariant ==
    /\ lcState \in States
    /\ retryCount \in Nat
    /\ phase \in Phases

NoEarlyPermanentFailure ==
    ~(phase = "done" /\ lcState = "failed" /\ retryCount = MaxRetries - 1)

AfterFirstRecoveryUsesFinalRetry ==
    phase = "after_first_recovery" => /\ lcState = "queued"
                                     /\ retryCount = MaxRetries

EventuallyFailsAtRetryBudget ==
    <>(phase = "done" /\ lcState = "failed" /\ retryCount = MaxRetries)

Spec ==
    Init /\ [][Next]_vars

HealthySpec ==
    Spec
        /\ WF_vars(Recover)
        /\ WF_vars(RestartRetriedAttempt)

=============================================================================
