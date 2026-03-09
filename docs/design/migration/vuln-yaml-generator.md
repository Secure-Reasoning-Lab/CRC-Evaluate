# Design: vuln.yaml Generator
- Audience: maintainers generating or regenerating CPV vulnerability metadata
- Scope: contracts for deriving `vuln.yaml` and related analysis artifacts from benchmark evidence
- Related: [Migration Validation](./migration-validation.md)

## Goals and Non-goals

### Goals
- define the inputs used to infer vulnerability metadata
- define the outputs and acceptance criteria for generated vulnerability records
- define failure semantics when evidence is incomplete or ambiguous

### Non-goals
- local command tutorials
- implementation snapshots of prompts or helper scripts
- provider-specific setup instructions

## Inputs

The generator may consume:
- crash logs
- POV inputs
- source context
- existing benchmark metadata
- optional repository context used to resolve locations or classify the issue

## Outputs

The generator may produce:
- canonical `vuln.yaml`
- supporting analysis notes
- generation logs or structured diagnostics

## Core Invariants

- generated vulnerability IDs must align with the owning CPV
- generated metadata must preserve benchmark-relative paths and location semantics
- generated output must be suitable for downstream hint generation and validation
- temporary/mock placeholders must be detectable and replaceable

## Failure Semantics

- missing required evidence produces an explicit generation failure or degraded-confidence result
- ambiguous CWE/location inference must be surfaced rather than hidden
- generator failure must not silently overwrite trusted vulnerability metadata with lower-quality output

## Decisions and Tradeoffs

- decision: derive metadata from existing benchmark evidence where possible
  - tradeoff: more dependence on artifact quality, less manual authoring
- decision: preserve explicit generation diagnostics
  - tradeoff: more output artifacts, better maintainer reviewability

## Validation

This contract should be covered by:
- vuln metadata generation tests
- CPV ID/path consistency tests
- placeholder/mock-detection tests

## Implementation Pointers

- vuln-yaml generator modules under `crsbench/migration/`
- migration-related test suites under `tests/`
