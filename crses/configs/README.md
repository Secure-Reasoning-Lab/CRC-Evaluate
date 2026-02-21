# Local CRS Configs

`crses/configs` stores **local deployment configuration** for CRS runs in CRSBench.

## Relationship to oss-crs

- Canonical CRS registry entries live in `oss-crs/registry`.
- `crses/configs/<name>/` provides local runtime settings for those registry entries.
- Keep secrets in `.env` files inside each config dir (gitignored).

## Typical Files

- `config-resource.yaml`: worker placement and resource limits
- `config-litellm.yaml`: model/provider wiring
- `config-worker.yaml`: optional worker overrides
- `.env`: local credentials/secrets

## Guidance

- Prefer deriving new configs from `oss-crs/example_configs` and adapting locally.
- Avoid duplicating CRS registry metadata (`name`, `source`, `type`) here.
- Keep this directory focused on deployment/runtime settings only.
