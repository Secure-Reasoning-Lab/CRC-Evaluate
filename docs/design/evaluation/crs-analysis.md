# Design: CRS Analysis
- Audience: maintainers working on post-run analysis of CRS behavior and artifacts
- Scope: optional analysis subsystem contracts for trial and snapshot data
- Related: [Snapshots](./snapshots.md), [Report Generation](../reporting/report-generation.md)

## Goals and Non-goals

### Goals
- define the contract for optional analyzers over CRS trial data
- preserve read-only analysis semantics
- support both snapshot-time and post-trial analysis consumers

### Non-goals
- implementation-specific plugin loading details
- roadmap/checklist tracking
- analysis visualization design

## Core Contract

The CRS analysis subsystem is optional and must not be required for successful trial execution.

Analyzers operate over already-produced trial artifacts and may emit derived insights, but they must not mutate CRS output or alter benchmark verdicts.

## Analyzer Contract

An analyzer must:
- accept a defined analysis input shape
- return structured analysis output
- tolerate missing optional data where possible
- fail without corrupting primary trial/evaluation artifacts

## Read-only Invariant

Analysis is observational only:
- no mutation of snapshots or trial outputs
- no mutation of benchmark verdicts
- no dependency on analysis success for primary experiment correctness

## Runtime Modes

### Snapshot-time analysis
Optional lightweight analysis may run against periodic snapshots.

### Post-trial analysis
More expensive analysis may run after trial completion using the full artifact set.

## Failure Semantics

- missing analyzer support must not fail trials
- analyzer-specific failure must be isolated and surfaced as analysis failure, not core evaluation failure
- malformed input artifacts are analysis input errors and should be reported as such

## Decisions and Tradeoffs

- decision: analyzers are optional
  - tradeoff: less guaranteed coverage, stronger isolation from core runtime correctness
- decision: analysis is read-only
  - tradeoff: less automation, simpler reasoning and safer downstream processing

## Validation

This contract should be covered by:
- analyzer interface tests
- analysis result serialization tests
- snapshot/trial integration tests for optional analysis paths

## Implementation Pointers

- analysis-related modules under `crsbench/`
- `tests/test_analysis_*`
