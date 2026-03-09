# Design: Dataset Module
- Audience: maintainers working on dataset distribution, upload/download behavior, and blind-evaluation packaging
- Scope: dataset contracts, artifact boundaries, backend semantics, and failure behavior
- Related: [Benchmark Data Protection and AI Contamination](../benchmark-protection-and-contamination.md)

## Goals and Non-goals

### Goals
- define how CRSBench packages and distributes benchmark datasets
- preserve blind-evaluation boundaries between benchmark content and ground truth
- support backend-specific transport without changing the dataset contract

### Non-goals
- operator command walkthroughs
- implementation snapshots of backend/client code
- storage-provider-specific deployment instructions

## Constraints

- GitHub is not a viable distribution path for the full benchmark corpus
- dataset distribution must preserve gated access and benchmark-protection goals
- the same dataset contract must support both current and future backends

## Context and Boundaries

The dataset subsystem sits between benchmark packaging and user download. It is
responsible for:
- publishing benchmark artifacts
- publishing or withholding ground-truth artifacts depending on use case
- routing dataset operations through a configured backend

It is not responsible for:
- benchmark creation itself
- runtime evaluation semantics after artifacts are downloaded

## Artifact Contract

Each benchmark dataset entry is split into two logical artifact classes:
- benchmark package content needed for execution
- ground-truth content needed for answer-aware workflows

This separation is normative because blind evaluation may require download of
the benchmark package without the answer-bearing artifact set.

## Registry Contract

Dataset resolution is registry-driven:
- dataset identifiers map to backend configuration
- benchmark prefixes or equivalent routing metadata determine which dataset owns
  a benchmark
- callers should not need backend-specific logic outside the dataset subsystem

## Backend Contract

Backends must provide the same semantic operations:
- publish packaged benchmark artifacts
- fetch benchmark artifacts by benchmark or suite selection
- preserve manifest/index information needed for incremental sync

Adding a backend must not require changes to user-facing dataset semantics.

## Manifest and Incremental Sync Contract

Both upload and download rely on source fingerprints or equivalent content
identity so unchanged benchmarks can be skipped safely.

Incremental behavior must satisfy:
- changed benchmark content is republished and re-fetched
- unchanged local content is not unnecessarily downloaded
- manifest/index state is explicit and inspectable

## Runtime Behavior

### Upload

Upload consumes packaged benchmark artifacts and publishes:
- benchmark content
- ground-truth content
- dataset metadata/index information
- canonical card/license files where required by the backend

### Download

Download supports:
- whole-dataset retrieval
- benchmark-scoped retrieval
- suite-scoped retrieval
- blind mode that omits ground-truth artifacts

## Failure Semantics

- backend resolution failure is a configuration error
- upload failure must not silently report a partial publication as complete
- download failure must not leave partially installed artifacts looking valid
- manifest/index mismatch must be surfaced explicitly

## Decisions and Tradeoffs

- decision: separate benchmark and ground-truth artifacts
  - tradeoff: more packaging complexity, correct blind-evaluation behavior
- decision: use registry-driven backend resolution
  - tradeoff: another indirection layer, cleaner backend extensibility
- decision: support incremental sync via explicit content identity
  - tradeoff: more metadata, better reliability and performance

## Risks and Validation

This contract should be validated by:
- bundling/unbundling tests
- backend dispatch tests
- incremental upload/download tests
- blind-evaluation download tests

## Implementation Pointers

- `crsbench/dataset/`
- dataset-related tests under `tests/`
