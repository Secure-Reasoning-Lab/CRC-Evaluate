---- MODULE DistributedAsyncPovVerifyDrain ----
EXTENDS TLC, Naturals

\* Code correspondence:
\* - POVVerificationManager._ensure_async_build_jobs() materializes the async
\*   POV build DAG once the first POV is discovered.
\* - POVVerificationManager._enqueue_pov() attaches build_job_ids plus RQ
\*   depends_on edges to each async POV verify job.
\* - distributed.evaluator_jobs.verify_single_pov() must consume prebuilt
\*   variants only; it must not hide a local build fallback on the verify worker.
\* - POVVerificationManager.drain_pending() must spend the full verify_timeout
\*   budget before marking pending POV verdicts as timed out.

CONSTANTS
    BuildDuration,
    VerifyDuration,
    VerifyTimeout,
    ShortDrainBudget,
    HiddenVerifyBuildBug,
    ShortDrainBug

RunnerPhases == {"trial_running", "draining"}
BuildStates == {"absent", "active", "done"}
VerifyStates == {"absent", "queued", "running", "done"}
Verdicts == {"pending", "success", "timeout"}

DrainBudget ==
    IF ShortDrainBug THEN ShortDrainBudget ELSE VerifyTimeout

VARIABLES
    phase,
    buildState,
    verifyState,
    buildRemaining,
    verifyRemaining,
    elapsed,
    hiddenBuildUsed,
    verdict

vars ==
    <<
        phase,
        buildState,
        verifyState,
        buildRemaining,
        verifyRemaining,
        elapsed,
        hiddenBuildUsed,
        verdict
    >>

Init ==
    /\ BuildDuration \in Nat
    /\ VerifyDuration \in Nat
    /\ VerifyTimeout \in Nat
    /\ ShortDrainBudget \in Nat
    /\ BuildDuration > 0
    /\ VerifyDuration > 0
    /\ VerifyTimeout > 0
    /\ ShortDrainBudget > 0
    /\ ShortDrainBudget < VerifyTimeout
    /\ BuildDuration + VerifyDuration <= VerifyTimeout
    /\ phase = "trial_running"
    /\ buildState = "absent"
    /\ verifyState = "absent"
    /\ buildRemaining = 0
    /\ verifyRemaining = 0
    /\ elapsed = 0
    /\ hiddenBuildUsed = FALSE
    /\ verdict = "pending"

DiscoverFirstPov ==
    /\ phase = "trial_running"
    /\ buildState = "absent"
    /\ verifyState = "absent"
    /\ phase' = phase
    /\ buildState' = "active"
    /\ verifyState' = "queued"
    /\ buildRemaining' = BuildDuration
    /\ verifyRemaining' = VerifyDuration
    /\ elapsed' = elapsed
    /\ hiddenBuildUsed' = hiddenBuildUsed
    /\ verdict' = verdict

StartDrain ==
    /\ phase = "trial_running"
    /\ verifyState = "queued"
    /\ phase' = "draining"
    /\ UNCHANGED
        <<
            buildState,
            verifyState,
            buildRemaining,
            verifyRemaining,
            elapsed,
            hiddenBuildUsed,
            verdict
        >>

StartVerifyWithHiddenLocalBuild ==
    /\ HiddenVerifyBuildBug
    /\ phase = "draining"
    /\ verifyState = "queued"
    /\ buildState # "done"
    /\ phase' = phase
    /\ buildState' = buildState
    /\ verifyState' = "running"
    /\ buildRemaining' = buildRemaining
    /\ verifyRemaining' = verifyRemaining
    /\ elapsed' = elapsed
    /\ hiddenBuildUsed' = TRUE
    /\ verdict' = verdict

Tick ==
    LET buildActive == buildState = "active"
        buildCompletes == buildState = "active" /\ buildRemaining = 1
        verifyConsumesTick ==
            \/ verifyState = "running"
            \/ /\ verifyState = "queued"
               /\ buildState = "done"
        verifyCompletes == verifyConsumesTick /\ verifyRemaining = 1
    IN
    /\ phase = "draining"
    /\ verdict = "pending"
    /\ elapsed < DrainBudget
    /\ phase' = phase
    /\ buildState' =
        IF buildCompletes THEN "done" ELSE buildState
    /\ verifyState' =
        IF verifyCompletes THEN "done"
        ELSE IF verifyConsumesTick THEN "running"
        ELSE verifyState
    /\ buildRemaining' =
        IF buildActive THEN buildRemaining - 1 ELSE buildRemaining
    /\ verifyRemaining' =
        IF verifyConsumesTick THEN verifyRemaining - 1 ELSE verifyRemaining
    /\ elapsed' = elapsed + 1
    /\ hiddenBuildUsed' = hiddenBuildUsed
    /\ verdict' =
        IF verifyCompletes THEN "success" ELSE verdict

DrainTimeout ==
    /\ phase = "draining"
    /\ verdict = "pending"
    /\ elapsed = DrainBudget
    /\ verifyState # "done"
    /\ phase' = phase
    /\ buildState' = buildState
    /\ verifyState' = verifyState
    /\ buildRemaining' = buildRemaining
    /\ verifyRemaining' = verifyRemaining
    /\ elapsed' = elapsed
    /\ hiddenBuildUsed' = hiddenBuildUsed
    /\ verdict' = "timeout"

Stutter ==
    UNCHANGED vars

Next ==
    \/ DiscoverFirstPov
    \/ StartDrain
    \/ StartVerifyWithHiddenLocalBuild
    \/ Tick
    \/ DrainTimeout
    \/ Stutter

TypeInvariant ==
    /\ phase \in RunnerPhases
    /\ buildState \in BuildStates
    /\ verifyState \in VerifyStates
    /\ buildRemaining \in Nat
    /\ verifyRemaining \in Nat
    /\ elapsed \in Nat
    /\ hiddenBuildUsed \in BOOLEAN
    /\ verdict \in Verdicts
    /\ buildState = "absent" => buildRemaining = 0
    /\ buildState = "done" => buildRemaining = 0
    /\ buildState = "active" => buildRemaining > 0
    /\ verifyState = "absent" => verifyRemaining = 0
    /\ verifyState = "queued" => verifyRemaining = VerifyDuration
    /\ verifyState = "running" => verifyRemaining > 0
    /\ verifyState = "done" => verifyRemaining = 0
    /\ phase = "trial_running" => elapsed = 0
    /\ verdict = "success" => verifyState = "done"
    /\ verdict = "timeout" => verifyState # "done"

VerifyStartsOnlyAfterBuildsComplete ==
    verifyState = "running" => buildState = "done"

NoHiddenVerifyLocalBuild ==
    ~hiddenBuildUsed

TimeoutUsesFullVerifyBudget ==
    verdict = "timeout" => elapsed >= VerifyTimeout

EventuallySucceedsWithinVerifyBudget ==
    <>(verdict = "success")

Spec ==
    Init /\ [][Next]_vars

HealthySpec ==
    Spec
        /\ WF_vars(DiscoverFirstPov)
        /\ WF_vars(StartDrain)
        /\ WF_vars(Tick)

=============================================================================
