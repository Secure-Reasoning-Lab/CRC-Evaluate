# Design: Trial Directory Preparation
- Audience: maintainers working on trial setup and runtime staging
- Scope: trial-directory contracts, staged artifacts, isolation rules, and failure semantics
- Related: [Evaluation](./evaluation.md), [Snapshots](./snapshots.md)

## Goals and Non-goals

### Goals
- define the directory and artifact contract for a prepared trial
- define what CRSBench stages before execution
- define isolation guarantees between trials

### Non-goals
- implementation walkthroughs of preparer helpers
- copied code blocks for directory builders
- operator command tutorials

## Core Contract

Each trial executes in an isolated directory tree that contains the runtime
inputs and output locations needed for one trial only. Trial preparation must be
sufficient for subsequent CRS execution without mutating unrelated trial state.

## Required Prepared Artifacts

A prepared trial may include:
- a trial root directory
- a build directory for tool/runtime build outputs
- an output directory for CRS-produced artifacts
- staged source context when the trial requires repository content
- staged hints when enabled by experiment config
- staged POV inputs when required by bug-fixing flows
- preparation metadata describing what was staged

## Isolation Invariants

- one trial must not write into another trial's build or output directories
- prepared source state for a trial must correspond to that trial's requested
  benchmark/mode/commit contract
- hint and POV staging must reflect only the inputs requested by experiment
  config and trial targeting
- preparation metadata must describe the prepared state accurately enough for
  debugging and post-run analysis

## Lifecycle

1. allocate/create the trial root
2. create required subdirectories
3. prepare source context if needed
4. stage hints/POVs/other trial inputs according to config
5. persist trial-preparation metadata
6. hand off to execution layer

## Failure Semantics

- missing required benchmark artifacts are preparation failures
- source checkout/staging failures are preparation failures
- optional inputs that are disabled must not be staged implicitly
- partial preparation must not be presented as a valid ready-to-run trial

## Distributed Considerations

Trial preparation may occur in queue-backed or non-local execution paths.
Consumers must not assume local-only filesystem state outside the prepared trial
root and declared shared infrastructure paths.

## Validation

This contract should be covered by:
- trial preparation tests
- source staging tests
- hint/POV staging tests
- distributed execution tests that consume prepared trials

## Implementation Pointers

- `crsbench/evaluation/trial_preparation.py`
- `tests/test_trial_preparation.py`
