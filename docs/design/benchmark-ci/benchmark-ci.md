# Design: Benchmark CI
- Audience: maintainers working on benchmark-validation pipelines and CI result semantics
- Scope: benchmark-CI contracts, job phases, artifact expectations, and failure semantics
- Related: [Unified Build & Verify](../distributed/unified-build-verify.md), [Benchmark CI guides](../../guides/benchmark-ci/README.md)

## Goals and Non-goals

### Goals
- define the benchmark-CI lifecycle for build and verification work
- define artifact/result semantics across CI jobs
- define how local and distributed CI execution preserve the same logical behavior

### Non-goals
- CRS experiment execution
- runnable operator command walkthroughs
- copied job-class implementations

## Core Contract

Benchmark CI validates benchmark integrity by executing a defined set of build and
verification operations over benchmark variants and expected artifacts. The same
logical job set must yield the same benchmark-level verdict semantics regardless
of whether execution is local or queue-backed.

## Phased Execution Model

Benchmark CI is organized into two logical phases:
1. build phase for required variants
2. verification/test phase for POV, patch, and optional coverage checks

Verification work may only consume variants whose required build context has been
completed or explicitly hydrated from durable build metadata.

## Artifact Contract

Benchmark CI may produce and consume:
- build outputs for benchmark variants
- reproduce/verification logs
- patch-test outputs
- coverage outputs when requested
- aggregate benchmark result summaries

Artifacts must remain attributable to the originating benchmark, variant, and job
class.

## Result Semantics

Aggregate CI results must distinguish:
- successful benchmark validation
- benchmark-quality failures (`FAIL`)
- infrastructure/dependency failures (`ERROR`)
- skipped or unavailable sub-results where the benchmark contract allows them

Dependency and infrastructure failures must not be silently collapsed into normal
benchmark failures.

## Queue and Execution Semantics

Benchmark-CI orchestration may run locally or through build/verify queues served
by evaluators. Queue selection and worker topology are operational concerns; the
contract here is that job identity, dependency ordering, and result aggregation
remain deterministic.

## Failure Semantics

- unknown or missing build context is an infrastructure failure
- dependency ordering violations are orchestration failures
- stale or missing queue/job metadata must surface as infrastructure errors
- partially available split results must not silently override authoritative
  combined results unless the contract explicitly allows fallback

## Validation

This contract should be covered by:
- CI DAG/job planning tests
- result aggregation tests
- distributed evaluator/CI integration tests
- infrastructure-failure classification tests

## Implementation Pointers

- `crsbench/benchmark_ci/`
- distributed CI/evaluator modules under `crsbench/distributed/`
- CI-related tests under `tests/`
