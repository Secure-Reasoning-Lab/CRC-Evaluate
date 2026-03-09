# CRS Configs Directory (`./crses`) — Deprecated Runtime Path

CRSBench now resolves `oss_crs_registry:` directly from `./oss-crs/registry/`.

## Current Contract

- Canonical CRS source: `./oss-crs/registry/`
- Experiment field `oss_crs_registry` must contain registry IDs
- `crs_compose.crs_services` is optional; when present, keys must be a subset of `oss_crs_registry`
- `crs_compose` + experiment-level resources control runtime placement/limits

## Status of `./crses`

`./crses` is no longer required for CRSBench runtime resolution.

You may keep it only for local notes/templates during migration. Runtime no longer
depends on `crses/configs/*/config-resource.yaml`.
