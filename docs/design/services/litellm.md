# LiteLLM Service Design

Audience: contributors modifying CRSBench's LiteLLM integration contract.

Related:
- [Environment Variables Reference](../../reference/environment-variables.md)
- [Configuration Guide](../../getting-started/configuration.md)
- [oss-crs Integration](../evaluation/oss-crs-integration.md)

## Purpose

This document defines the CRSBench contract for using LiteLLM as the LLM access
layer for CRS execution.

## Scope

Covered here:
- runtime contract CRSBench expects from LiteLLM-related configuration
- trust boundaries between CRS containers, CRSBench runtime, and upstream
  LiteLLM services
- accounting/tracking semantics used by CRSBench

Not covered here:
- deployment tutorials
- provider-specific operational setup
- container/runtime implementation details for helper scripts

## Supported Runtime Model

CRSBench experiment runtime currently treats external LiteLLM as the canonical
supported mode. CRSBench resolves the LiteLLM endpoint and credentials from the
runtime configuration and `CRSBENCH_LLM_*` environment contract.

## Contract Boundary

The relevant actors are:
- CRSBench runtime, which resolves endpoint and credentials
- CRS containers, which consume the resolved endpoint contract
- upstream LiteLLM service, which authenticates, forwards, and optionally tracks
  usage

CRSBench does not define provider-specific semantics. It expects an
OpenAI-compatible endpoint with the configured authentication contract.

## Configuration Contract

Canonical CRSBench-facing variables are the `CRSBENCH_LLM_*` names documented in
reference docs. Runtime configuration controls:
- whether LiteLLM participation is skipped entirely
- whether usage tracking/accounting is required
- which endpoint CRSBench should treat as authoritative

When usage tracking is enabled, credentials required for upstream key management
or accounting must be present before execution starts.

## Invariants

- CRSBench must resolve one unambiguous LiteLLM endpoint for a given trial.
- Runtime credential resolution must prefer canonical `CRSBENCH_LLM_*` inputs.
- Tracking/accounting settings must not silently degrade into a less strict mode.
- Trial execution must not depend on provider API keys being directly visible to
  CRS containers when the configured runtime model uses an upstream gateway.

## Failure Semantics

- Missing required LiteLLM endpoint configuration is a setup failure.
- Missing tracking credentials when tracking is enabled is a setup failure.
- Provider-side or upstream LiteLLM request failures are runtime execution
  failures and should surface as such, not as configuration success.

## Validation

Changes here require coverage for:
- canonical environment resolution
- skip/enable behavior
- tracking-enabled credential enforcement
- adapter/runtime propagation of resolved LiteLLM settings

## Implementation Pointers

Implementation is split between environment/config resolution, orchestration, and
adapter/runtime invocation paths under `crsbench/`.
