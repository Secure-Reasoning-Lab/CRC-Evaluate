# Experiment Configs

## Directory Contract

- Maximum depth is one subdirectory level:
  - allowed: `experiment-configs/<file>.yaml`
  - allowed: `experiment-configs/<group>/<file>.yaml`
  - not allowed: `experiment-configs/<group>/<subgroup>/<file>.yaml`
- AFC final canonical groups:
  - `afc-final-bugfinding/`
  - `afc-final-bugfixing/`
  - `sanity-bugfinding/`
  - `sanity-bugfixing/`
  - `cloud-testing/`

## Naming Convention

- Filenames in this directory are a readability convention only.
- CRSBench does not enforce filename schemas; only YAML content is validated.
- Keep names descriptive and stable for operators (for example `{crs}-{model}-{mode}-...`).

Examples:
- `afc-final-bugfinding/atlantis-multilang-given_fuzzer-default-full-given-fuzzer-run-1.yaml`
- `afc-final-bugfinding/atlantis-multilang-given_fuzzer-default-full-distributed-localhost.yaml`
- `afc-final-bugfixing/crs-codex-gpt-5-4-full.yaml`
- `afc-final-bugfixing/crs-claude-code-claude-sonnet-4-20250514-full.yaml`
- `cloud-testing/gce-usenix-r1-1orch-2worker-1eval-multilang-given-fuzzer.yaml`
- `sanity-bugfinding/atlantis-multilang-given_fuzzer-default-delta.yaml`
- `sanity-bugfixing/crs-copilot-cli-gpt-5-3-codex-delta-sanity-mock-c.yaml`

## Legacy Files

- Legacy top-level AFC presets (`experiment-config-afc*.yaml`) and `paper-eval/*.yaml` were removed.
- New AFC/sanity experiments should use only:
  - `afc-final-bugfinding/`
  - `afc-final-bugfixing/`
  - `sanity-bugfinding/`
  - `sanity-bugfixing/`

## Config Schema Contract

- Use `crs_compose` CRS service keys as the CRS source-of-truth.
- Use `crs_compose` (do not use `crs_overrides`).
- Use either `benchmarks` or `benchmark_suite` (mutually exclusive).
- Set `experiment.task` explicitly:
  - `bugfinding`
  - `bugfixing`
- Keep shared templates machine-agnostic when possible.
- Use explicit `runtime.inputs` knobs (no implicit difficulty-based behavior):
  - `runtime.inputs.pov`
  - `runtime.inputs.sarif`
  - `runtime.inputs.seed`
  - `runtime.inputs.diff`
- Task constraints:
  - `task: bugfinding` only supports `runtime.inputs.pov`
  - `runtime.inputs.sarif/seed/diff` are for `task: bugfixing`

Preferred `crs_compose` shape:
- `crs_compose.oss_crs_infra` with exactly one CPU mode:
  - `num_cores`
  - `shared: true`
- Optional per-CRS overrides:
  - `crs_compose.<crs-name>` where `<crs-name>` is the registry ID key
- Optional fields:
  - `mem_limit`
  - `additional_env`

Smoke/CI bug-finding presets may explicitly set `runtime.pov_early_stop: true`
to terminate once all expected CPVs for a harness are confirmed. Keep the
global/default behavior `false` for non-smoke experiment configs.

## Validation

- Validate config schema via local checks:
  - `scripts/ci-tests/run-local.sh checks`
- Smoke selected scenarios:
  - `scripts/ci-tests/run-local.sh smoke`
  - successful smoke suites also rerun top-level `verify` / `patch-verify`
    against the generated trial outputs before the suite is marked passed
