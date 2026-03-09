# Design: Logging Architecture
- Audience: maintainers working on CRSBench logging behavior and integration
- Scope: logging contracts, invariants, configuration boundaries, and migration expectations
- Related: [Logging Reference](../../reference/logging.md)

## Goals and Non-goals

### Goals
- define the centralized logging contract for CRSBench modules
- define formatting and configuration invariants
- define compatibility expectations for legacy `logging`-style call sites

### Non-goals
- operator logging tutorials
- runnable shell workflows
- implementation snapshots of logger internals

## Core Contract

CRSBench uses a centralized logger abstraction as the single source of truth for:
- log-level filtering
- module-path formatting
- color/TTY behavior
- runtime reconfiguration

All modules should emit logs through the shared logger surface rather than ad hoc
module-local logging configuration.

## Invariants

- log formatting is consistent across modules
- non-TTY output is free of ANSI color codes
- TTY output may include level-aware colorization
- runtime log-level overrides apply uniformly across modules
- centralized logger configuration must not require each module to call its own
  setup routine

## Module Path Semantics

The logging layer normalizes module identity into concise hierarchical labels so
operators can distinguish subsystems such as distributed execution, evaluation,
migration, and benchmark-CI without reading Python import paths directly.

## Compatibility Contract

The logging architecture should continue to support migration from standard
`logging` usage by providing a clear adapter path for legacy call sites. The
contract is compatibility of emitted semantics, not preservation of prior API
shapes.

## Failure Semantics

- logger configuration failure must not silently suppress critical logs
- invalid runtime configuration should degrade to safe defaults rather than
  leaving the process without logging
- log formatting must remain readable when color support is unavailable

## Testing Expectations

This contract should be covered by:
- logger configuration tests
- TTY/non-TTY formatting tests
- level filtering tests
- compatibility/migration tests for legacy call sites

## Implementation Pointers

- `crsbench/utils/logger.py`
- `tests/test_logger.py`
