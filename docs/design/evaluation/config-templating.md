# CRS Compose Runtime Configuration

## Status

Current and active design. This replaces the old `crs_overrides` proposal.

## Overview

CRSBench configures oss-crs runtime via `experiment_config.crs_compose`.

- Input surface (CRSBench): `num_cores` / `shared`, optional `mem_limit`, optional `additional_env`
- Output surface (generated for oss-crs): concrete `cpuset` and optional `memory`

CRSBench computes CPU slices per trial and generates `crs-compose.yaml` consumed by `oss-crs`.

## Experiment Config Shape

`crs_compose` supports flat CRS keys:

```yaml
crs_compose:
  oss_crs_infra:
    # Choose exactly one CPU mode:
    # num_cores: 8
    shared: true
    # Optional; omit for unlimited
    # mem_limit: 16G

  crs-claude-code:
    num_cores: 8
    # Optional; omit for unlimited
    # mem_limit: 16G
    additional_env:
      ANTHROPIC_MODEL: claude-opus-4-6

  crs-codex:
    num_cores: 8
    additional_env:
      CODEX_MODEL: gpt-5.3-codex
```

Nested `crs_services` is still accepted for compatibility, but flat keys are the preferred format.

## Infra CPU Modes

`oss_crs_infra` must set exactly one CPU mode.

1. `num_cores: N` (dedicated infra cores)
- CRSBench reserves `N` cores from the trial CPU pool for infra.
- Remaining cores are assigned to CRS services.

2. `shared: true` (shared infra cores)
- No dedicated infra reservation.
- Infra cpuset is the union of CRS service cpusets in that trial.

## CPU Allocation Rules

Given a trial CPU allocation (from worker/evaluator scheduler), CRSBench allocates in order:

1. infra dedicated slice (only when `num_cores` mode)
2. each CRS service slice by configured `num_cores`

If required cores exceed allocated trial cores, the trial fails fast with an insufficient CPU error.

### Example

Allocated trial CPUs: `40-55` (16 cores)

- Dedicated mode:
  - `oss_crs_infra.num_cores: 4`
  - `crs-a.num_cores: 6`
  - `crs-b.num_cores: 6`
  - Result:
    - infra: `40-43`
    - crs-a: `44-49`
    - crs-b: `50-55`

- Shared mode:
  - `oss_crs_infra.shared: true`
  - `crs-a.num_cores: 8`
  - `crs-b.num_cores: 8`
  - Result:
    - crs-a: `40-47`
    - crs-b: `48-55`
    - infra: `40-55` (union)

## Memory Rules

- `mem_limit` is optional on both infra and CRS services.
- If omitted, CRSBench does not emit `memory` for that container in generated compose.
- Effective behavior is container runtime default (no explicit CRSBench memory cap).

## Generated oss-crs Compose

CRSBench converts the model above into oss-crs compose format:

- `oss_crs_infra.cpuset`, optional `oss_crs_infra.memory`
- per-CRS `<crs-name>.cpuset`, optional `<crs-name>.memory`
- `additional_env` forwarded per CRS, with framework-managed keys added/overridden as needed (for example `SANITIZER`, `FUZZING_LANGUAGE`).

## Validation Rules

CRSBench validates at config parse time:

- `crs_compose` CRS keys must exactly match `crses`
- `oss_crs_infra` must set exactly one of:
  - `num_cores`
  - `shared: true`
- `num_cores` values must be positive for CRS services
- unknown keys are rejected (except flat CRS key normalization)

## Migration Notes

From legacy config:

- `crs_overrides` -> removed
- `crs_compose.crs_services.<name>` -> prefer `crs_compose.<name>`
- `mem_limit` -> now optional

## Implementation Pointers

- Schema: `crsbench/validation/schemas.py`
- Adapter CPU assignment + compose generation: `crsbench/evaluation/adapter/oss_crs.py`
- Compose YAML model: `crsbench/evaluation/adapter/config_gen.py`
