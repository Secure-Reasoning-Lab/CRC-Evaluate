---- MODULE DistributedTerminalMarkers ----
EXTENDS TLC

\* Code correspondence:
\* - workers publish `.success` / `.fail` in distributed/jobs.py
\* - orchestrator publishes `.success` / `.fail` in run_experiment.py
\* - writers must replace the opposite terminal marker, not accumulate both

CONSTANT ClearsOppositeMarker

Actors == {"worker", "orchestrator"}
MarkerVals == {"success", "fail"}
WriterVals == Actors \cup {"none"}

VARIABLES markers, lastWriter

vars == <<markers, lastWriter>>

Init ==
    /\ markers = {"fail"}
    /\ lastWriter = "none"

PublishSuccess(actor) ==
    /\ actor \in Actors
    /\ markers' =
        IF ClearsOppositeMarker
            THEN {"success"}
            ELSE markers \cup {"success"}
    /\ lastWriter' = actor

PublishFail(actor) ==
    /\ actor \in Actors
    /\ markers' =
        IF ClearsOppositeMarker
            THEN {"fail"}
            ELSE markers \cup {"fail"}
    /\ lastWriter' = actor

Stutter ==
    UNCHANGED vars

Next ==
    \/ \E actor \in Actors: PublishSuccess(actor)
    \/ \E actor \in Actors: PublishFail(actor)
    \/ Stutter

TypeInvariant ==
    /\ markers \subseteq MarkerVals
    /\ lastWriter \in WriterVals

NoTerminalMarkerContradiction ==
    ~(("success" \in markers) /\ ("fail" \in markers))

Spec ==
    Init /\ [][Next]_vars

=============================================================================
