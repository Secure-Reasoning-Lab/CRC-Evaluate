---- MODULE DistributedStaleLockResume ----
EXTENDS TLC

\* Code-correspondent model for `crsbench run --queue-mode continue` when the
\* previous orchestrator left a stale experiment lock behind.
\*
\* Python correspondence:
\* - continue mode first tries `register_or_raise()`
\* - if lock acquisition fails, the orchestrator should attempt
\*   `resume_or_raise()` to reclaim a stale lock
\* - only if resume also fails should the controller abort without mutating the
\*   queue

CONSTANT TryResumeEnabled

LockStates == {"stale_locked", "resumed", "aborted"}
Phases == {"initial", "recovery"}

VARIABLES lockState, phase, mutatedQueue

vars == <<lockState, phase, mutatedQueue>>

Init ==
    /\ lockState = "stale_locked"
    /\ phase = "initial"
    /\ mutatedQueue = FALSE

RegisterFails ==
    /\ lockState = "stale_locked"
    /\ phase = "initial"
    /\ UNCHANGED vars

ResumeStaleLock ==
    /\ TryResumeEnabled
    /\ lockState = "stale_locked"
    /\ phase = "initial"
    /\ lockState' = "resumed"
    /\ UNCHANGED <<phase, mutatedQueue>>

AbortWithoutResume ==
    /\ lockState = "stale_locked"
    /\ phase = "initial"
    /\ ~TryResumeEnabled
    /\ lockState' = "aborted"
    /\ UNCHANGED <<phase, mutatedQueue>>

RecoverExistingQueueState ==
    /\ lockState = "resumed"
    /\ phase = "initial"
    /\ phase' = "recovery"
    /\ mutatedQueue' = TRUE
    /\ UNCHANGED lockState

Stutter ==
    UNCHANGED vars

Next ==
    \/ RegisterFails
    \/ ResumeStaleLock
    \/ AbortWithoutResume
    \/ RecoverExistingQueueState
    \/ Stutter

TypeInvariant ==
    /\ lockState \in LockStates
    /\ phase \in Phases
    /\ mutatedQueue \in BOOLEAN

StaleLockCanRecover ==
    lockState = "aborted" => FALSE

ResumeEnablesRecovery ==
    phase = "recovery" => /\ lockState = "resumed"
                          /\ mutatedQueue

EventuallyRecoversFromStaleLock ==
    <>(phase = "recovery")

Spec ==
    Init /\ [][Next]_vars

HealthySpec ==
    Spec
        /\ WF_vars(ResumeStaleLock)
        /\ WF_vars(RecoverExistingQueueState)

=============================================================================
