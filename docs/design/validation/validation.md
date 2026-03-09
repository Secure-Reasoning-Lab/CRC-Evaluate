# Design: Validation
- Audience: maintainers working on benchmark and experiment schema/semantic validation
- Scope: validation contracts and issue semantics
- Related: [RFC](../../RFC.md), [Experiment Config Reference](../../guides/experiments/config-reference.md)

## Goals and Non-goals

### Goals
- define the validation guarantees for benchmark and experiment inputs
- define issue/result semantics for callers, agents, and CI
- keep validation pure, deterministic, and safe to invoke repeatedly

### Non-goals
- end-user command tutorials
- implementation snapshots of validators or models
- duplication of every schema field from canonical reference docs

## Core Contract

Validation in CRSBench is:
- read-only
- deterministic for the same inputs
- structured enough for machine consumption
- able to accumulate multiple issues in one pass where safe

## Input Classes

The validation layer covers:
- benchmark metadata and benchmark-local artifacts
- experiment configuration
- benchmark-suite manifests
- selected packaging/runtime invariants that must be caught before execution

## Result Contract

A validation result must provide:
- overall validity
- structured issues with severity and code
- field/path context where applicable
- summary counts
- serializable output suitable for tooling and agents

## Issue Semantics

### Error
An error blocks execution or publication because the input violates a required contract.

### Warning
A warning highlights risky or non-ideal configuration that does not necessarily block execution.

## Validation Phases

1. existence/readability checks
2. syntax/parsing checks
3. schema/type checks
4. semantic/invariant checks
5. best-practice warnings

Later phases may be skipped when earlier phases make them meaningless, but the validator should preserve as much actionable feedback as correctness allows.

## Agent and Tooling Contract

The validation API is intentionally suitable for agent/tool use because it is:
- side-effect free
- fast enough for repeated invocation
- serializable
- explicit about errors vs warnings

## Failure Semantics

- malformed or unreadable inputs produce structured file/parse errors
- schema failures identify the invalid field/path when available
- semantic failures identify the violated invariant
- validators must not partially mutate repository state while reporting issues

## Decisions and Tradeoffs

- decision: keep validation pure-function and read-only
  - tradeoff: less auto-repair, safer tooling and concurrency
- decision: structured issue codes
  - tradeoff: more maintenance, better machine consumption and CI reporting
- decision: validate early before execution
  - tradeoff: more up-front checks, less runtime ambiguity

## Validation

This contract should be covered by:
- benchmark validation tests
- experiment-config schema and semantic tests
- benchmark-suite validation tests
- integration tests against canonical example docs/configs

## Implementation Pointers

- `crsbench/validation.py`
- `tests/test_validation.py`
