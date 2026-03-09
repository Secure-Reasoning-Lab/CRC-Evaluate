# Design: test.sh Generator
- Audience: maintainers working on benchmark functional-test generation
- Scope: contracts for generating `test.sh` artifacts and related analysis outputs
- Related: [Migration Validation](./migration-validation.md)

## Goals and Non-goals

### Goals
- define the generator inputs, outputs, and invariants
- define how repository context and benchmark metadata influence generated test artifacts
- define failure semantics for agent-assisted and container-assisted generation flows

### Non-goals
- shell walkthroughs for local use
- implementation snapshots of agent prompts or helper scripts
- provider-specific model setup tutorials

## Inputs

The generator consumes:
- benchmark metadata
- project repository context
- detected build/test-system information
- optional container-validation capabilities

## Outputs

The generator may emit:
- `test.sh`
- supporting analysis notes or logs
- structured generation outcome metadata

## Core Invariants

- generated scripts must be benchmark-scoped and reproducible from the same repository state
- generator output must remain compatible with containerized benchmark execution
- container-validation/refinement steps must not silently change benchmark semantics

## Runtime Modes

### Standard generation
Analyze repository context and emit candidate `test.sh` plus supporting notes.

### Iterative/container-assisted generation
Optionally build and validate generated artifacts in a container loop, refining until the artifact satisfies the generator's acceptance criteria or fails with an explicit reason.

## Failure Semantics

- missing repository context is a generation precondition failure
- unsupported or ambiguous test/build layouts are reported explicitly
- container-validation failures must be surfaced as generation failures, not hidden retries

## Decisions and Tradeoffs

- decision: keep generation benchmark-scoped
  - tradeoff: more repeated work, simpler artifact provenance
- decision: support optional iterative/container-assisted refinement
  - tradeoff: more complexity, better artifact validation before handoff

## Validation

This contract should be covered by:
- generator unit tests
- repository-context tests
- optional container-validation tests where applicable

## Implementation Pointers

- migration/test-sh generator modules under `crsbench/`
- related migration/test agent tests under `tests/`
