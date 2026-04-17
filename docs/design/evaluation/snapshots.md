# Design: Snapshots
- Audience: maintainers working on trial snapshot capture and consumption
- Scope: snapshot format, lifecycle, and consumption contracts
- Related: [Evaluation](./evaluation.md), [Report Generation](../reporting/report-generation.md)

## Goals and Non-goals

### Goals
- define what a snapshot contains
- define when snapshots are captured and how they are consumed
- define failure and partial-data semantics

### Non-goals
- implementation walkthroughs of snapshot manager internals
- checklist-style feature tracking
- report-format design details

## Snapshot Contract

A snapshot is a point-in-time read-only capture of trial progress. It must be sufficient for downstream consumers to:
- reconstruct progress over time
- inspect CRS outputs available at that capture point
- aggregate time-series metrics
- distinguish partial progress from final outcome

## Required Snapshot Semantics

A snapshot may include:
- cycle or sequence identity
- timestamp and elapsed runtime context
- discovered POV/patch state at capture time
- selected CRS artifact metadata
- token/cost/resource metadata when available

The exact storage representation may evolve, but these semantics are stable.

## Lifecycle

1. snapshot thread/process is initialized with the running trial
2. captures occur periodically according to runtime policy
3. snapshots are persisted atomically enough for later discovery
4. downstream consumers load snapshots after or during trial execution

## Partial and Final State

- snapshots before trial completion are partial by definition
- downstream consumers must not interpret a partial snapshot as a final verdict
- final reporting may depend on the complete snapshot set plus terminal trial artifacts

## Failure Semantics

- snapshot capture failure must not silently corrupt trial state
- snapshot-loading consumers must tolerate missing/partial snapshot sequences and report that explicitly
- malformed snapshot content is an input error for downstream consumers

## Distributed Considerations

Snapshots may be produced alongside distributed trial execution. Consumers must not assume synchronized arrival of snapshots, queue results, and final aggregate artifacts.

## Cadence-Coupled Side Effects

Other trial-level controls that need a periodic observation point share the
snapshot cadence rather than running their own timer:

- POV early-stop evaluates CPV completion on each cycle
- per-CRS trial budget policy polls LiteLLM key state on each cycle

These controls are cooperative: they signal the trial's stop event and may
lag by up to one snapshot cycle. Lowering `snapshot_period` tightens both
reaction windows uniformly.

## Decisions and Tradeoffs

- decision: snapshots are read-only progress captures
  - tradeoff: simpler reasoning, less ability to “repair” trial state in place
- decision: keep snapshot semantics stable even if storage format evolves
  - tradeoff: compatibility constraints, better downstream tooling stability

## Validation

This contract should be covered by:
- snapshot manager tests
- snapshot-loading tests
- time-series/reporting integration tests

## Implementation Pointers

- `crsbench/snapshot_manager.py`
- `tests/` snapshot-related suites
