# Design: Report Generation
- Audience: maintainers working on report synthesis from experiment artifacts
- Scope: report-generation contracts, inputs, outputs, and invariants
- Related: [Snapshots](../evaluation/snapshots.md), [Evaluation](../evaluation/evaluation.md)

## Goals and Non-goals

### Goals
- define how reporting consumes snapshots and aggregate trial artifacts
- define stable report contents across output formats
- define validation and failure semantics for reporting runs

### Non-goals
- implementation snapshots of report generators
- operator CLI walkthroughs
- dashboard styling details

## Inputs

Reporting consumes:
- experiment directories and trial metadata
- snapshot archives and extracted snapshot metadata
- evaluation and verification result artifacts
- optional cost/token usage metadata

Inputs are read-only.

## Output Contract

Report generation must be able to produce:
- experiment-level aggregate reports
- optional trial-level reports
- machine-readable formats
- human-readable formats

Different output formats may differ in presentation, but they must preserve the same underlying metrics and verdict semantics.

## Core Invariants

- the same snapshot/result set yields the same metrics
- report generation does not modify source experiment artifacts
- validation of experiment completeness is explicit and reportable
- missing or partial artifacts are surfaced as reporting/validation problems, not silently ignored

## Metric Contract

Reports may aggregate:
- success/failure/error counts
- POV and patch discovery/verification outcomes
- time-series progress across snapshots
- token/cost metrics when available

Metric definitions must remain stable across output formats.

## Runtime Behavior

1. discover experiment/trial scope
2. validate completeness and artifact availability
3. load snapshots and result artifacts
4. aggregate metrics
5. emit report artifacts in requested formats

## Failure Semantics

- invalid experiment path is a reporting error
- unreadable or malformed snapshot data is a reporting error
- partial experiment state may still support validation-only reporting, but must be explicit
- format generation failures must not corrupt other successfully generated outputs

## Distributed Considerations

Reporting is typically post-execution, but it must tolerate artifacts produced by distributed runs where snapshots, verify results, and queue outcomes were generated asynchronously.

## Decisions and Tradeoffs

- decision: reporting is read-only over experiment artifacts
  - tradeoff: safer reproducibility, less opportunity for auto-repair
- decision: shared metric definitions across formats
  - tradeoff: tighter coupling between generators, consistent interpretation

## Validation

This contract should be covered by:
- snapshot-loading tests
- metrics aggregation tests
- report generation tests
- experiment completeness validation tests

## Implementation Pointers

- `crsbench/reporting/`
- `tests/` reporting-related suites
