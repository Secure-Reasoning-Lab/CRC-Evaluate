# Design: Evaluation
- Audience: maintainers working on CRS execution, result aggregation, and verify semantics
- Scope: evaluation contracts for benchmark execution and result aggregation
- Related: [Snapshots](./snapshots.md), [Patch Verification](./patch-verification.md), [Distributed Evaluation](../distributed/distributed-evaluation.md)

## Goals and Non-goals

### Goals
- define the evaluation lifecycle for bug-finding and bug-fixing execution
- define result-shape contracts and aggregate semantics
- define evaluation behavior across local and distributed execution paths

### Non-goals
- implementation mirroring of runner/adapter classes
- CLI tutorials
- exhaustive schema duplication from runtime config docs

## Context and Boundaries

The evaluation subsystem bridges:
- benchmark metadata and harness definitions
- CRS adapter lifecycle (`prepare`, `build-target`, `run`)
- POV/patch verification and aggregate reporting

It consumes validated experiment/benchmark inputs and produces structured per-harness and aggregate results.

## Lifecycle Contract

For each benchmark harness, evaluation follows this lifecycle:
1. resolve benchmark mode and harness context
2. configure the CRS adapter
3. prepare/build target as required by the adapter lifecycle
4. execute the CRS run phase
5. interpret run outputs into per-POV or per-patch results
6. aggregate harness-level results into benchmark-level output

## Adapter Contract

The adapter boundary must provide:
- explicit mode (`bug-finding` or `bug-fixing`)
- a stable prepare/build-target/run lifecycle
- deterministic output interpretation for the selected mode
- enough metadata for downstream reporting and verification

The concrete adapter implementation may change, but callers depend on these lifecycle guarantees.

## Result Contracts

### POVStatus
Canonical per-POV states:
- `FOUND`
- `MISSED`
- `ERROR`

### POVResult
Must capture:
- POV identity
- harness/sanitizer context
- status
- execution time
- optional error/output details

### HarnessResult
Must capture:
- harness identity
- overall success/error state
- total harness execution time
- list of associated POV or verification results

### EvaluationReport
Must capture:
- benchmark identity and mode
- summary counts and success/error metrics
- per-harness results
- configuration provenance
- total execution time
- serializable output form for reporting

## Aggregate Semantics

- infrastructure/dependency failures must remain distinguishable from benchmark-result failures
- split POV/Patch checks are authoritative only when the full split set exists
- partial split data may fall back to combined legacy checks where supported
- aggregate status gives infrastructure `ERROR` precedence over ordinary `FAIL` when the failure is not benchmark-semantic

## Distributed / CI Semantics

- evaluator `--ci` behavior is a compatibility alias on top of the unified evaluator path
- CI-specific behavior is queue selection, not a separate evaluation model
- build context and verify context must remain consistent across remote nodes
- verify-side cache hydration must not silently violate build/verify ownership

## Failure Semantics

- invalid benchmark/config state aborts before CRS execution
- CRS execution errors propagate into structured result states rather than ambiguous missing output
- missing dependency/build context is an infrastructure failure
- stale queue metadata or missing queued jobs is an infrastructure failure in distributed paths

## Decisions and Tradeoffs

- decision: keep evaluation centered on an adapter lifecycle
  - tradeoff: more abstraction, cleaner support for multiple CRS behaviors
- decision: preserve explicit `ERROR` vs `FAIL`
  - tradeoff: more result complexity, better operational diagnosis
- decision: use structured per-harness aggregation
  - tradeoff: richer data model, clearer reporting and verification behavior

## Validation

This contract should be covered by:
- adapter branching tests
- evaluation result-shape tests
- split-vs-combined result fallback tests
- distributed evaluator/CI result-semantics tests

## Implementation Pointers

- `crsbench/evaluation/runner.py`
- `crsbench/evaluation/adapter/`
- `crsbench/evaluation/results.py`
