# CRS Configs in CRSBench

CRSBench uses **oss-crs** as the source of truth for CRS registry entries.

## Source of Truth

- Canonical registry: `oss-crs/registry`
- Local deployment configs: `crses/configs`

## Recommended Layout

```text
oss-crs/
  registry/                  # Canonical CRS entries (*.yaml)

crses/
  configs/
    <config-name>/
      config-resource.yaml   # Worker placement + resources
      config-litellm.yaml    # LiteLLM model/provider configuration
      config-worker.yaml     # Optional per-worker overrides
      .env                   # Local secrets (gitignored)
```

## How CRSBench Resolves CRS

1. Experiment config lists CRS config names (for example `crs-libfuzzer`).
2. CRSBench reads `crses/configs/<name>/config-resource.yaml` to resolve registry name(s).
3. CRS type/source is loaded from `registry_dir`.
   - Default: `oss-crs/registry`

## Experiment Config Defaults

- `registry_dir`: `oss-crs/registry`
- `crs_configs_dir`: `crses/configs`

You can override both in experiment YAML when needed.

## What to Keep in This Repo

Keep in `crses/configs` only:
- deployment-specific resource placement
- local environment settings
- worker-specific overrides

Do **not** duplicate upstream registry definitions unless you intentionally need
legacy compatibility.

## Important

`crses/registry` is no longer used. CRS registry definitions must come from
`oss-crs/registry`.
