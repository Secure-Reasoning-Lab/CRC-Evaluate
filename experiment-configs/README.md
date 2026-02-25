# Experiment Configs

Store experiment configurations as:

- `experiment-config-{name}.yaml` for regular runs
- `paper-eval/*.yaml` for paper/repro pipelines

Notes:

- Keep machine-local paths out of shared templates when possible.
- Use `scripts/ci-tests/smoke-manifest.yaml` and `scripts/ci-tests/run-local.sh smoke` for smoke/integration-style test scenarios.
