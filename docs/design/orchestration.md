# Design: Experiment Orchestration
- Audience: maintainers working on experiment startup, trial expansion, and local/distributed mode selection
- Scope: orchestration contracts for `crsbench run`
- Related: [Architecture](./architecture.md), [Distributed Job Queue](./distributed/distributed-job-queue.md), [Experiment Guides](../guides/experiments/README.md)

## Goals and Non-goals

### Goals
- define how experiment configs become concrete trial execution units
- define local vs distributed mode selection semantics
- document orchestration boundaries with workers, evaluators, and reporting

### Non-goals
- runnable CLI tutorials
- implementation-level function snapshots
- duplication of config schema details owned by the config reference

## Context and Boundaries

The orchestration layer is responsible for:
- loading and validating experiment configuration
- expanding the trial matrix
- selecting local or distributed execution
- coordinating result aggregation and report generation

It is not responsible for benchmark-specific build/verify internals; those are delegated to evaluation and distributed subsystems.

## Configuration Contract

The experiment config is the source of truth for:
- experiment identity
- benchmark selection
- CRS service selection and compose metadata
- runtime timeouts and input contracts
- storage locations

CLI flags may influence execution control, but they do not replace experiment-definition fields owned by config.

## Trial Expansion Contract

A concrete trial is defined by:
- CRS service
- benchmark harness
- mode
- sanitizer
- trial number
- optional CPV target for bug-fixing flows

Expansion must be deterministic for the same config and benchmark set.

Within each CRS, expansion is wavefront-ordered across concrete variant streams
keyed by:
- benchmark
- harness
- mode
- sanitizer
- optional CPV target

Each stream remains FIFO by `trial_num`, but the final matrix emits `trial 1`
across all discovered streams before `trial 2`. Stream discovery order remains
deterministic and is preserved; only the emission order changes from
stream-at-a-time to wavefront-by-trial.

## Mode Selection Contract

### Local execution
Local execution is used when:
- explicitly forced locally, or
- only one concrete trial exists, or
- distributed prerequisites are absent/unavailable

### Distributed execution
Distributed execution is used when:
- explicitly requested and queue infrastructure is configured and reachable, or
- multiple concrete trials exist and the distributed prerequisites are satisfied

Conflicting explicit mode flags must be rejected.

## Runtime Behavior

### Local path
- run expanded trials in-process
- consume the expanded trial matrix in order without additional reordering
- aggregate results directly
- produce final summary/report artifacts without queue mediation

### Distributed path
- register runtime metadata
- resolve queue and retry policy
- enqueue deterministic trial jobs
- consume the expanded trial matrix in order without additional reordering
- monitor terminal outcomes
- aggregate results after queue-backed execution completes

## Failure Semantics

- invalid config aborts before trial expansion
- conflicting execution flags abort before runtime start
- unavailable distributed prerequisites force an explicit error when distributed mode was requested
- queue/orchestration infrastructure failures are distinct from benchmark-result failures
- completed-on-disk or already-queued work may be filtered according to queue/retry policy

## Distributed / Non-local Considerations

- orchestrator must not assume workers share local process state
- trial/job payloads must fully describe the work needed on remote nodes
- filtering of existing work must be safe under concurrent workers/evaluators
- result aggregation must tolerate partial or retryable infrastructure failure

## Decisions and Tradeoffs

- decision: config-first experiment definition
  - tradeoff: less CLI flexibility, better reproducibility
- decision: local fast path for minimal work
  - tradeoff: two execution paths, lower overhead for single-trial cases
- decision: deterministic trial expansion and IDs
  - tradeoff: stricter modeling, better aggregation and retry safety

## Validation

This contract should be exercised by:
- trial-expansion tests
- local/distributed mode-selection tests
- distributed queue monitoring tests
- config validation tests

## Implementation Pointers

- `crsbench/run_experiment.py`
- `crsbench/distributed/jobs.py`
- `crsbench/distributed/queue.py`
