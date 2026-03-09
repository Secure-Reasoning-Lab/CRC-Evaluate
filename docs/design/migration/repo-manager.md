# Design: Repository Manager
- Audience: maintainers working on migration and repository-backed generation flows
- Scope: repository acquisition, reuse, and checkout contracts for migration tooling
- Related: [Atlanta-to-RFC Migration](./migration-atlanta-to-rfc.md), [test.sh Generator](./test-sh-generator.md)

## Goals and Non-goals

### Goals
- define how migration tooling locates, clones, reuses, and checks out project repositories
- define reproducibility and cache semantics for repository-backed workflows
- define failure handling for missing or inconsistent repository state

### Non-goals
- git usage tutorials
- implementation snapshots of clone/checkout helpers
- provider-specific authentication setup guides

## Core Contract

The repository manager provides benchmark- and migration-facing tooling with a consistent repository context by:
- locating existing local repositories when appropriate
- cloning repositories when required
- checking out the requested revision or benchmark-defined source state
- reusing cached repositories where safe

## Invariants

- repository state supplied to callers must correspond to the requested revision
- cached reuse must not silently violate reproducibility
- repository acquisition failures must be explicit and actionable
- callers must be able to distinguish “not found”, “clone failed”, and “checkout failed” cases

## Cache and Reuse Semantics

- local cache reuse is allowed when repository identity and requested revision remain compatible
- stale or divergent repository state must trigger explicit refresh/checkout handling
- migration and generation tools must not assume a repository exists until the manager confirms it

## Failure Semantics

- missing repository metadata is a caller-input failure
- clone failure is distinct from checkout failure
- partial repository state must not be treated as ready-to-use context
- non-local or provider authentication problems must surface as acquisition failures, not silent fallbacks

## Decisions and Tradeoffs

- decision: centralize repository acquisition logic
  - tradeoff: one more abstraction, less duplicated and inconsistent cloning behavior
- decision: support cache reuse with explicit revision enforcement
  - tradeoff: more cache-state handling, better performance without sacrificing reproducibility

## Validation

This contract should be covered by:
- clone/reuse/checkout tests
- failure-mode tests for missing or invalid metadata
- migration/generator integration tests that depend on repository context

## Implementation Pointers

- repository manager modules under `crsbench/migration/`
- migration and generator test suites under `tests/`
