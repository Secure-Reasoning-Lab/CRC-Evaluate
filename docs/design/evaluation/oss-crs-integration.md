# Design: oss-crs Integration
- Audience: maintainers working on CRSBench orchestration of `oss-crs`
- Scope: integration boundaries, parameter contracts, staged inputs, and failure semantics
- Related: [Evaluation](./evaluation.md), [Path Resolver](./path-resolver.md), [Trial Directory Preparation](./trial-directory-preparation.md)

## Goals and Non-goals

### Goals
- define how CRSBench supplies benchmark/trial context to `oss-crs`
- define which inputs are staged by CRSBench versus consumed directly by `oss-crs`
- define integration failure semantics across build and run phases

### Non-goals
- reproducing `oss-crs` CLI documentation
- runnable command tutorials
- copied code from adapters or repository managers

## Integration Boundary

CRSBench is responsible for preparing benchmark- and trial-specific context,
then invoking `oss-crs` with that prepared state. `oss-crs` is responsible for
executing CRS-specific build/run behavior within its own contract.

## Inputs Provided by CRSBench

Depending on workflow, CRSBench may provide:
- benchmark/project path information
- prepared source context
- prepared trial/build/output directories
- resolved harness-source paths
- staged hints or POV inputs
- registry/config references for CRS selection

## Invariants

- the prepared trial/build context must correspond to the selected benchmark,
  harness, mode, and sanitizer contract
- staged source and input artifacts must be reproducible from benchmark metadata
- integration failures must remain attributable to either CRSBench staging or
  downstream `oss-crs` execution

## Failure Semantics

- missing prepared inputs are CRSBench-side integration failures
- invalid CRS registry/config references are integration failures
- downstream `oss-crs` build/run failures must surface distinctly from staging
  failures
- partial outputs must not be mistaken for successful end-to-end trial execution

## Distributed Considerations

In distributed execution, workers and evaluators may consume previously staged or
cached artifacts. The integration contract must not assume a single long-lived
local process with in-memory state.

## Validation

This contract should be covered by:
- adapter integration tests
- staged-input preparation tests
- distributed execution tests that exercise `oss-crs` handoff paths

## Implementation Pointers

- `crsbench/evaluation/adapter/`
- `crsbench/evaluation/`
- integration-focused tests under `tests/`
