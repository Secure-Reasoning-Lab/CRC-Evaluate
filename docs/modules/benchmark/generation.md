# Benchmark Generation

Module scope: future benchmark-generation capabilities that create CRSBench
benchmark structures from upstream vulnerability sources.

## Current State

This module area is reserved for future generation workflows. CRSBench does not
currently expose a mature benchmark-generation subsystem here.

## Intended Responsibility

When implemented, benchmark generation should cover:
- source ingestion from supported vulnerability inputs
- normalization into CRSBench benchmark structure
- handoff into packaging/validation workflows for release readiness

## Out of Scope

This page does not define:
- contributor benchmark-authoring workflow
- benchmark packaging contract
- runtime benchmark loading behavior

## Canonical References

- [Benchmark Developer Guide](../../contributors/benchmark-developer-guide.md)
- [Benchmark RFC](../../RFC.md)

## Implementation Pointers

- future benchmark-generation entrypoints are expected to live under the
  benchmark CLI surface when that subsystem is implemented
