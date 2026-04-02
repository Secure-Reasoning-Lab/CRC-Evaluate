---- MODULE DistributedSessionMarkerStability ----
EXTENDS TLC

\* Code-correspondent model for shared monitor callbacks in one controller
\* session:
\* - no canonical marker exists initially
\* - one physical job reports success and writes the marker
\* - a later duplicate physical job for the same logical trial reports failure
\* - the session must preserve the first canonical verdict rather than rewrite it

CONSTANT PreserveSessionCanonicalMarker

Markers == {"none", "success", "fail"}
Reports == {"none", "success", "fail"}

VARIABLES marker, firstReport, secondReport

vars == <<marker, firstReport, secondReport>>

Init ==
    /\ marker = "none"
    /\ firstReport = "none"
    /\ secondReport = "none"

WriteFirstSuccess ==
    /\ firstReport = "none"
    /\ firstReport' = "success"
    /\ secondReport' = secondReport
    /\ marker' = "success"

ObserveSecondConflict ==
    /\ firstReport = "success"
    /\ secondReport = "none"
    /\ secondReport' = "fail"
    /\ firstReport' = firstReport
    /\ marker' =
        IF PreserveSessionCanonicalMarker THEN
            marker
        ELSE
            "fail"

Next ==
    \/ WriteFirstSuccess
    \/ ObserveSecondConflict
    \/ UNCHANGED vars

TypeInvariant ==
    /\ marker \in Markers
    /\ firstReport \in Reports
    /\ secondReport \in Reports

SessionCanonicalMarkerSticky ==
    secondReport = "fail" => marker = "success"

Spec ==
    Init /\ [][Next]_vars

=============================================================================
