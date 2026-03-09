# Design: Path Resolver
- Audience: maintainers working on harness/source path resolution for evaluation
- Scope: path-resolution contracts, supported path forms, and failure semantics
- Related: [oss-crs Integration](./oss-crs-integration.md), [Trial Directory Preparation](./trial-directory-preparation.md)

## Goals and Non-goals

### Goals
- define the supported harness/source path forms
- define how benchmark-relative and repository-relative paths are resolved
- define resolution failure behavior for callers

### Non-goals
- shell tutorials for CRS invocation
- copied implementation of resolver helpers
- Docker-mount walkthroughs

## Supported Path Forms

The path resolver may accept path expressions that are:
- repository-relative
- project-relative
- benchmark-relative
- already-absolute when explicitly allowed by the runtime contract

The resolver's job is to produce a stable host-side path usable by downstream
execution tooling.

## Core Contract

Resolution must be:
- deterministic for the same benchmark/repository inputs
- explicit about whether a result is usable or unresolved
- compatible with repository-manager and benchmark-layout contracts

## Resolution Invariants

- resolved paths must correspond to the intended benchmark/repository context
- failed resolution must not silently fall back to an unrelated path
- callers must be able to distinguish invalid syntax from missing filesystem
  targets
- repository acquisition and path resolution remain separate concerns even when
  the resolver depends on repository state

## Failure Semantics

- invalid path expressions are caller-input errors
- missing repository or project context is a resolution failure
- missing resolved files are explicit not-found failures
- callers may choose whether resolution failure is fatal or warning-level, but
  the resolver itself must preserve the distinction

## Integration Contract

The resolver is consumed by evaluation/adapter layers that pass source-context
paths to CRS runtime tooling. It returns resolved paths or explicit failure so
those layers can decide whether to continue, warn, or abort.

## Validation

This contract should be covered by:
- resolver unit tests for each supported path form
- repository-context tests
- caller integration tests for fallback/error behavior

## Implementation Pointers

- `crsbench/evaluation/path_resolver.py`
- path-resolver related tests under `tests/`
