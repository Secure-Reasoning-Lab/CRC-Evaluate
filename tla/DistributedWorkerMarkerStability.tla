---- MODULE DistributedWorkerMarkerStability ----
EXTENDS TLC

\* Code-correspondent model for worker-side terminal publication:
\* - the physical worker attempt passes the ownership fence for its own job_id
\* - a canonical marker already exists for the logical trial
\* - a late duplicate physical attempt reports the opposite verdict
\* - worker-side publication must preserve the existing canonical marker

CONSTANT PreserveExistingCanonicalMarker

Markers == {"success", "fail"}
WorkerReports == {"none", "success", "fail"}

VARIABLES marker, workerReport, ownerCurrent

vars == <<marker, workerReport, ownerCurrent>>

Init ==
    /\ marker = "success"
    /\ workerReport = "none"
    /\ ownerCurrent = TRUE

PublishConflictingWorkerResult ==
    /\ ownerCurrent
    /\ workerReport = "none"
    /\ workerReport' = "fail"
    /\ ownerCurrent' = ownerCurrent
    /\ marker' =
        IF PreserveExistingCanonicalMarker THEN
            marker
        ELSE
            "fail"

Next ==
    \/ PublishConflictingWorkerResult
    \/ UNCHANGED vars

TypeInvariant ==
    /\ marker \in Markers
    /\ workerReport \in WorkerReports
    /\ ownerCurrent \in BOOLEAN

ExistingCanonicalMarkerSticky ==
    workerReport = "fail" => marker = "success"

Spec ==
    Init /\ [][Next]_vars

=============================================================================
