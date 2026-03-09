# Design: Unified Build & Verify Architecture
- Audience: maintainers working on build/verify execution across CRSBench workflows
- Scope: unified queue-backed build/verify contracts, variant planning, caching, and async verification semantics
- Related: [Distributed Evaluation](./distributed-evaluation.md), [Benchmark CI](../benchmark-ci/benchmark-ci.md)

## Goals and Non-goals

### Goals
- define the common build/verify execution model across benchmark-CI and experiment verification flows
- centralize variant-planning and build-result semantics
- define async verification behavior and cache expectations

### Non-goals
- preserving legacy thread-pool or DAGExecutor implementation details
- runnable operator examples
- copied implementation snapshots of planners, queues, or caches

## Core Contract

Build and verify work use one logical execution model: queue-backed jobs served
by evaluator processes. Different entrypoints may submit different job sets, but
variant planning, build-result semantics, and verification-result semantics are
shared.

## Queue Model

The architecture distinguishes:
- trial queues for CRS execution
- build queues for variant creation
- verify queues for POV/patch verification

Build and verify may share evaluator capacity, but build completion remains a
precondition for dependent verification work.

## Variant Planning Contract

Variant planning is the single source of truth for which benchmark variants must
be built. Planning must be deterministic from benchmark metadata and requested
workflow semantics.

## Build Result Cache Contract

Build-result caching may be used to avoid redundant work, but:
- cache identity must match benchmark and variant semantics
- cache reuse must not change verification correctness
- stale or incompatible cache state must be detectable and bypassable

## CI Phase Semantics

Benchmark-CI uses a build phase followed by verification/test phases. The
barrier between phases is logical: verification jobs may only rely on completed
or explicitly hydrated build context.

## Async Verification During CRS Runs

When CRS execution discovers verification candidates during a run, verification
may be enqueued asynchronously so worker processes are not blocked on heavy build
or reproduce work. Polling/early-stop behavior must remain eventually
consistent, not necessarily immediate.

## Failure Semantics

- missing build context is an infrastructure failure
- failed or incompatible cache hydration is explicit, not silent fallback
- per-POV verification failures must stay isolated from unrelated candidates
- asynchronous verification must not silently disappear if queue submission or
  polling fails

## Validation

This contract should be covered by:
- variant-planner tests
- build-cache tests
- benchmark-CI phase tests
- async POV verification integration tests

## Implementation Pointers

- `crsbench/executor/`
- `crsbench/distributed/`
- build/verify related tests under `tests/`
