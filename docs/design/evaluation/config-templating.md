# CRS Compose Runtime Configuration

Audience: contributors changing `crs_compose` schema, CPU/memory assignment, or oss-crs compose generation.
Scope: runtime configuration contract from experiment config into generated oss-crs compose files.

## Overview

CRSBench configures oss-crs runtime via `experiment_config.crs_compose`.

- Input surface (CRSBench): `num_cores` / `shared`, optional `mem_limit`, optional `additional_env`
- Output surface (generated for oss-crs): concrete `cpuset` and explicit `memory`

CRSBench computes CPU slices per trial and generates `crs-compose.yaml` consumed by `oss-crs`.
Each trial runs a single CRS. If multiple CRS services are declared under `crs_compose`, CRSBench creates separate trials per CRS.

## Experiment Config Shape

`crs_compose` uses top-level CRS service keys:

```yaml
crs_compose:
  oss_crs_infra:
    # Choose exactly one CPU mode:
    # num_cores: 8
    shared: true
    # Optional; omit to use adapter default memory policy
    # mem_limit: 16G

  crs-claude-code:
    num_cores: 8
    # Optional; omit to use adapter default memory policy
    # mem_limit: 16G
    additional_env:
      ANTHROPIC_MODEL: claude-opus-4-6

    # You may define multiple CRS entries, but only the current trial CRS entry
    # is applied when generating the compose for that trial.
```

## Infra CPU Modes

`oss_crs_infra` must set exactly one CPU mode.

1. `num_cores: N` (dedicated infra cores)
- CRSBench reserves `N` cores from the trial CPU pool for infra.
- CRSBench assigns service cores by configured `crs_compose.<current_crs>.num_cores`.
- If the current CRS service override is absent, adapter fallback may use only a subset of remaining trial cores.

2. `shared: true` (shared infra cores)
- No dedicated infra reservation.
- Infra cpuset is the union of CRS service cpusets in that trial.

## CPU Allocation Rules

Given a trial CPU allocation (from worker/evaluator scheduler), CRSBench allocates in order:

1. infra dedicated slice (only when `num_cores` mode)
2. current trial CRS slice from `crs_compose.<current_crs>` by `num_cores` (if provided)

If required cores exceed allocated trial cores, the trial fails fast with an insufficient CPU error.

### Example

Allocated trial CPUs: `40-55` (16 cores), current trial CRS = `crs-a`

- Dedicated mode:
  - `oss_crs_infra.num_cores: 4`
  - `crs-a.num_cores: 12`
  - Result:
    - infra: `40-43`
    - crs-a: `44-55`

- Shared mode:
  - `oss_crs_infra.shared: true`
  - `crs-a.num_cores: 16`
  - Result:
    - crs-a: `40-55`
    - infra: `40-55` (union)

## Memory Rules

- `mem_limit` is optional on both infra and CRS services.
- If omitted, adapter defaults are used, and generated compose still carries an explicit memory value.
- Effective behavior is not unlimited by default; default memory is resolved by adapter policy/env.

## Generated oss-crs Compose

CRSBench converts the model above into oss-crs compose format:

- `oss_crs_infra.cpuset`, `oss_crs_infra.memory`
- per-CRS `<crs-name>.cpuset`, `<crs-name>.memory`
- `additional_env` forwarded per CRS, with framework-managed keys added/overridden as needed (for example `SANITIZER`, `FUZZING_LANGUAGE`).

## Validation Rules

CRSBench validates at config parse time:

- CRS entries are declared directly under `crs_compose`
- `oss_crs_infra` must set exactly one of:
  - `num_cores`
  - `shared: true`
- `num_cores` values must be positive for CRS services
- unknown keys are rejected

## Implementation Pointers

- Schema: `crsbench/validation/schemas.py`
- Adapter CPU assignment + compose generation: `crsbench/evaluation/adapter/oss_crs.py`
- Compose YAML model: `crsbench/evaluation/adapter/config_gen.py`
