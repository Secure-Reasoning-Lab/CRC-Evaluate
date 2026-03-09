# Design: Patch Verification
- Audience: maintainers working on patch-validation and security-verdict behavior
- Scope: patch-verification contracts, accepted artifacts, verdict semantics, and failure behavior
- Related: [Evaluation](./evaluation.md), [oss-crs Integration](./oss-crs-integration.md)

## Goals and Non-goals

### Goals
- define the accepted patch/POV artifact layouts
- define patch-verification lifecycle and verdict semantics
- define failure and cache behavior at a contract level

### Non-goals
- runnable CLI walkthroughs
- copied dataclass/API implementations
- implementation snapshots of verification helpers

## Inputs

Patch verification consumes:
- candidate patch artifacts
- corresponding POV artifacts
- benchmark and harness context
- build/cache context
- optional incremental-build artifacts when available

## Accepted Artifact Layouts

Patch inputs may be provided as:
- a single explicit patch/POV pair
- a directory-structured patch/POV set
- flat patch sets when the target mapping remains inferable by the verifier

CRS outputs and benchmark ground-truth artifacts must remain distinguishable.

## Verification Lifecycle

1. resolve and validate patch/POV inputs
2. apply patch to the intended source context
3. build with the selected build strategy
4. run POV verification against the target CPV context
5. run the configured test mode
6. emit verification status and security verdict

## Security Verdict Contract

A patch receives a security verdict based on whether the verifier completed the
required POV validation successfully. Test-only success without required POV
validation is not a passing security verdict.

## Status Semantics

Verification status must distinguish at least:
- precondition/input error
- build/application failure
- POV still triggers
- tests failed
- verification passed

## Cache and Incremental-Build Semantics

- cached build artifacts may be reused when they match the requested contract
- force-rebuild semantics must bypass cache reuse explicitly
- incremental-build use is an optimization, not a semantic change in the final
  verification contract
- fallback from incremental build to standard build must remain explicit

## Failure Semantics

- patch-application failure is distinct from build failure
- malformed or unmappable input layout is an input error
- missing required build context is an infrastructure/integration failure
- partial verification must not be reported as a full success

## Validation

This contract should be covered by:
- patch-verification tests for status/verdict semantics
- build-cache and force-rebuild tests
- input-layout and mapping tests
- distributed patch-verification integration tests where applicable

## Implementation Pointers

- patch-verification modules under `crsbench/evaluation/`
- patch-verification test suites under `tests/`
