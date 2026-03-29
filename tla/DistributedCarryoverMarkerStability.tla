---- MODULE DistributedCarryoverMarkerStability ----
EXTENDS TLC

\* Code-correspondent model for continue-mode monitoring of contradictory
\* terminal duplicates after restart:
\* - a canonical marker already exists on disk for the logical trial
\* - a stale carryover physical job reports the opposite terminal verdict
\* - restart monitoring must not overwrite the existing canonical marker

CONSTANT PreserveExistingCanonicalMarker

Markers == {"success", "fail"}
TerminalReports == {"none", "success", "fail"}

VARIABLES marker, report

vars == <<marker, report>>

Init ==
    /\ marker = "success"
    /\ report = "none"

ObserveConflictingCarryover ==
    /\ report = "none"
    /\ report' = "fail"
    /\ marker' =
        IF PreserveExistingCanonicalMarker THEN
            marker
        ELSE
            "fail"

Next ==
    \/ ObserveConflictingCarryover
    \/ UNCHANGED vars

TypeInvariant ==
    /\ marker \in Markers
    /\ report \in TerminalReports

ExistingCanonicalMarkerSticky ==
    report = "fail" => marker = "success"

Spec ==
    Init /\ [][Next]_vars

=============================================================================
