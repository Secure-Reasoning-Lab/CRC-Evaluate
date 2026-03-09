# Build-Verify Architecture

Audience: contributors changing benchmark-CI execution and evaluator behavior.

Related:
- [Benchmark CI Design](./benchmark-ci.md)
- [Unified Build/Verify](../distributed/unified-build-verify.md)
- [Patch Verification](../evaluation/patch-verification.md)

## Purpose

This document defines the contract between build production and verification
consumption across benchmark-CI and standalone verification paths.

## Scope

Covered here:
- build mode semantics (`snapshot` vs `full`)
- cache and rebuild semantics
- build-context handoff from build jobs to verify jobs
- patch build identity and deduplication

Not covered here:
- operator command walkthroughs
- benchmark-specific outcomes
- queue topology details beyond the build/verify contract

## Build Modes

### Full mode

Full mode builds variants from the normal OSS-Fuzz/base-image path. It is the
baseline path for standalone verification-style commands.

### Snapshot mode

Snapshot mode uses incremental-image preparation when available and is the
default execution model for modular benchmark-CI DAG commands. Snapshot mode may
fall back to full mode when an incremental image is unavailable or unusable.

## Cache Contract

A cached build may be reused only when all of the following match the current
request:
- benchmark/variant identity
- requested build mode
- relevant execution metadata needed to distinguish incompatible outputs

`--force-rebuild` invalidates cache reuse for the requested variant and requires
fresh build output.

## Verification Consumption Contract

Verification paths must treat build output as a producer-consumer boundary:
- build workers produce build artifacts and build-context metadata
- verify workers consume those artifacts and metadata
- verify workers must not silently rebuild missing variants on the verification
  path when the workflow requires a build/verify split

For distributed execution, verify-side cache hydration is allowed from durable
on-disk build metadata when in-memory state is absent. Hard failure is correct
only when the required variant is genuinely missing.

## Patch Build Identity

Patch build and patch verification jobs must use deterministic identities that
include the fields that materially affect output correctness, including:
- benchmark + harness + CPV + patch identity
- execution mode / source-mode fields
- sanitizer or equivalent runtime distinctions
- variant-selection flags that change produced outputs

This identity contract prevents duplicate concurrent work and ensures verify jobs
consume the correct upstream patch build.

## Invariants

- A verification result must correspond to the exact build mode requested or its
  explicitly permitted fallback.
- Snapshot-mode fallback to full mode must be visible in result metadata.
- Verify jobs must never consume ambiguous build outputs.
- Patch verification must reference the real upstream patch-build identity, not a
  locally reconstructed approximation.

## Failure Semantics

- Incremental-image unavailability is not itself a hard failure when the
  contract permits fallback to full build.
- Missing build context on the verify side is an infrastructure failure, not a
  benchmark result.
- Duplicate patch-build submission must deduplicate to a single authoritative
  producer rather than racing concurrent copies.

## Validation

Changes here require coverage for:
- mode-sensitive cache reuse and force-rebuild behavior
- verify-side build-context hydration on cold workers
- deterministic patch-build identity and deduplication
- snapshot fallback metadata reporting

## Implementation Pointers

Primary implementation lives in benchmark-CI orchestration, distributed job
submission/consumption, and evaluation build/verify paths under `crsbench/`.
