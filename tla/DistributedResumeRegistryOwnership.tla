--------------------------- MODULE DistributedResumeRegistryOwnership ---------------------------
EXTENDS TLC

CONSTANTS ResumeAdoptsExistingRegistration, ResumeRepublishesMissingRegistration

VARIABLES registryPresent, cleanupOwnsRegistration, cleanedUp, resumed

Init ==
    /\ registryPresent \in BOOLEAN
    /\ cleanupOwnsRegistration = FALSE
    /\ cleanedUp = FALSE
    /\ resumed = FALSE

ResumeTakeover ==
    /\ ~resumed
    /\ registryPresent' =
        IF registryPresent
            THEN TRUE
            ELSE ResumeRepublishesMissingRegistration
    /\ cleanupOwnsRegistration' =
        IF registryPresent
            THEN ResumeAdoptsExistingRegistration
            ELSE ResumeRepublishesMissingRegistration
    /\ cleanedUp' = FALSE
    /\ resumed' = TRUE

Cleanup ==
    /\ resumed
    /\ ~cleanedUp
    /\ cleanedUp' = TRUE
    /\ registryPresent' =
        IF cleanupOwnsRegistration
            THEN FALSE
            ELSE registryPresent
    /\ UNCHANGED <<cleanupOwnsRegistration, resumed>>

Next ==
    ResumeTakeover
    \/ Cleanup
    \/ UNCHANGED <<registryPresent, cleanupOwnsRegistration, cleanedUp, resumed>>

CleanupClearsRegistryAfterResume ==
    ~(resumed /\ cleanedUp /\ registryPresent)

Spec ==
    Init /\ [][Next]_<<registryPresent, cleanupOwnsRegistration, cleanedUp, resumed>>

=============================================================================
