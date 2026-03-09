# Design: Hint Levels
- Audience: maintainers working on hint staging and experiment input contracts
- Scope: SARIF hint-level semantics, aggregation rules, and runtime delivery contracts
- Related: [Trial Directory Preparation](./trial-directory-preparation.md), [Snapshots](./snapshots.md)

## Goals and Non-goals

### Goals
- define the meaning of supported hint levels
- define how hint artifacts are selected and staged for trials
- define non-leaky aggregation expectations for delivered hint artifacts

### Non-goals
- command-line walkthroughs for hint generation
- implementation snapshots of hint staging helpers
- future placeholder configuration that is not part of the current runtime contract

## Core Contract

Hint delivery is controlled by experiment-config inputs. When SARIF hints are
enabled, CRSBench selects the configured level and stages only the matching hint
artifacts for the active benchmark/trial context.

## SARIF Hint-Level Semantics

Hint levels progress from coarse vulnerability-class information toward more
detailed location/context information. Higher levels may include more precise
location metadata, but they must remain bounded by the benchmark's intended hint
contract.

## Staging Invariants

- only the configured SARIF level is delivered for a given staged hint artifact
- staged hint filenames and layout must not leak benchmark-internal CPV identity
  unless the benchmark contract explicitly allows it
- hint staging must aggregate across the applicable trial context without
  silently including disabled hint sources

## Failure Semantics

- missing configured hint artifacts are explicit staging failures or warnings,
  depending on caller policy
- malformed hint artifacts are input errors
- disabled hint inputs must result in no staged hint output

## Future Compatibility

Additional hint-source types may be introduced later, but this document covers
only the active staged-hint contract. Future extensions must preserve the same
config-driven, non-leaky delivery principles.

## Validation

This contract should be covered by:
- experiment-config schema tests for hint input selection
- hint staging tests for level selection and non-leaky naming
- trial preparation integration tests

## Implementation Pointers

- hint preparation logic under `crsbench/evaluation/`
- hint-related validation and trial-preparation tests under `tests/`
