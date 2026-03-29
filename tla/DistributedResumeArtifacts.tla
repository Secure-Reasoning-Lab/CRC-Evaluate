---- MODULE DistributedResumeArtifacts ----
EXTENDS TLC

\* Code-correspondent model for restart reconciliation when terminal artifacts
\* already exist on disk.
\*
\* Python correspondence:
\* - lifecycle may still say `syncing` after a controller restart
\* - published `.success` / `.fail` markers are authoritative
\* - `reconcile_on_resume()` should collapse artifact-backed in-flight work to a
\*   terminal lifecycle state before asking for collection follow-up

CONSTANT ResumeChecksArtifacts

LCStates == {"running", "syncing", "completed", "failed"}
Artifacts == {"none", "success", "fail"}
Stages == {"live", "restarted", "reconciled"}

VARIABLES lcState, artifact, needsCollection, stage

vars == <<lcState, artifact, needsCollection, stage>>

Init ==
    /\ lcState = "running"
    /\ artifact = "none"
    /\ needsCollection = FALSE
    /\ stage = "live"

BeginSyncing ==
    /\ stage = "live"
    /\ lcState = "running"
    /\ lcState' = "syncing"
    /\ UNCHANGED <<artifact, needsCollection, stage>>

PublishSuccessArtifact ==
    /\ stage = "live"
    /\ lcState = "syncing"
    /\ artifact' = "success"
    /\ UNCHANGED <<lcState, needsCollection, stage>>

PublishFailArtifact ==
    /\ stage = "live"
    /\ lcState = "syncing"
    /\ artifact' = "fail"
    /\ UNCHANGED <<lcState, needsCollection, stage>>

RestartController ==
    /\ stage = "live"
    /\ artifact # "none"
    /\ stage' = "restarted"
    /\ UNCHANGED <<lcState, artifact, needsCollection>>

ResumeReconcile ==
    /\ stage = "restarted"
    /\ stage' = "reconciled"
    /\ IF ResumeChecksArtifacts
          THEN /\ lcState' = IF artifact = "success" THEN "completed" ELSE "failed"
               /\ needsCollection' = FALSE
          ELSE /\ lcState' = lcState
               /\ needsCollection' = (lcState = "syncing")
    /\ UNCHANGED artifact

Stutter ==
    UNCHANGED vars

Next ==
    \/ BeginSyncing
    \/ PublishSuccessArtifact
    \/ PublishFailArtifact
    \/ RestartController
    \/ ResumeReconcile
    \/ Stutter

TypeInvariant ==
    /\ lcState \in LCStates
    /\ artifact \in Artifacts
    /\ needsCollection \in BOOLEAN
    /\ stage \in Stages

NoArtifactBackedSyncingAfterResume ==
    /\ stage = "reconciled"
    /\ artifact # "none"
    => /\ lcState \in {"completed", "failed"}
       /\ ~needsCollection

ArtifactEventuallyBecomesTerminal ==
    artifact # "none" => <>(lcState \in {"completed", "failed"})

Spec ==
    Init /\ [][Next]_vars

HealthySpec ==
    Spec
        /\ WF_vars(BeginSyncing)
        /\ WF_vars(RestartController)
        /\ WF_vars(ResumeReconcile)
        /\ WF_vars(PublishSuccessArtifact)
        /\ WF_vars(PublishFailArtifact)

=============================================================================
