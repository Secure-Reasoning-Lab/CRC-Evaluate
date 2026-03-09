# Benchmark Lifecycle
- Audience: maintainers working on benchmark creation, packaging, publication, and runtime loading
- Scope: lifecycle contracts across generation, packaging, distribution, and execution-time loading
- Related: [RFC](../../RFC.md), [Dataset Module](../dataset/dataset.md)

## Goals and Non-goals

### Goals
- define the canonical lifecycle phases for CRSBench benchmarks
- define what each phase must produce for the next phase
- keep runtime loading behavior compatible with packaged benchmark artifacts

### Non-goals
- command tutorials for packaging or validation
- implementation snapshots of packaging helpers
- source-specific generation playbooks

## Lifecycle Phases

The benchmark lifecycle has four contract phases:
1. generation
2. packaging
3. publish/distribution
4. runtime loading

Each phase consumes the previous phase's artifacts and must preserve benchmark
identity, harness identity, and CPV semantics.

## Generation Contract

Generation creates a benchmark directory from an upstream vulnerability source
or manual input. A generated benchmark must already satisfy the CRSBench
benchmark shape closely enough that packaging and validation can proceed without
guessing intent.

Generation remains a separate concern from distribution and runtime loading.

## Packaging Contract

Packaging transforms a benchmark into a distributable form while preserving:
- source provenance
- benchmark metadata
- runtime-usable source layout
- delta/full mode semantics where applicable

For packaged source, the produced artifact must match the runtime loader's
expectations for directory naming and source availability.

## Publish Contract

Publication distributes packaged benchmarks through a dataset or equivalent
distribution channel. Published artifacts must preserve:
- reproducibility/provenance information
- packaging integrity
- any blind-evaluation separation between runnable content and ground truth

## Runtime Loading Contract

Runtime loading resolves the source material needed for evaluation. The loader
must be able to distinguish between:
- bundled source already present in the benchmark package
- source that must be materialized or cloned at runtime

Callers must be able to reason about whether an explicit source path is needed.

## Cross-Phase Invariants

- harness/CPV identity must not drift between phases
- packaging must not introduce runtime-only assumptions that generation never
  guaranteed
- runtime loading must honor the packaged benchmark contract rather than infer a
  different source layout opportunistically
- provenance must remain available for audit and reproduction

## Failure Semantics

- generation failure means no benchmark is ready for packaging
- packaging failure means no distributable artifact is ready for publication
- publication failure must not claim dataset availability prematurely
- runtime load failure must distinguish missing bundled source from clone/load
  failures

## Decisions and Tradeoffs

- decision: keep lifecycle phases explicit
  - tradeoff: more subsystem boundaries, much clearer contracts
- decision: make packaging/runtime compatibility normative
  - tradeoff: tighter requirements on packaged output, fewer runtime surprises
- decision: preserve provenance through the lifecycle
  - tradeoff: more metadata, better reproducibility

## Risks and Validation

This contract should be validated by:
- packaging validation tests
- tarball/source layout tests
- runtime loader tests
- end-to-end tests covering packaged benchmark execution

## Implementation Pointers

- `crsbench/benchmark/generation/`
- `crsbench/benchmark/packaging/`
- `crsbench/benchmark/runtime/`
- benchmark-related tests under `tests/`
