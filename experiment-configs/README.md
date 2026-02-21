# Experiment Configs

Store experiment configurations as:

- `experiment-config-{name}.yaml` for regular runs
- `paper-eval/*.yaml` for paper/repro pipelines

Notes:

- Keep machine-local paths out of shared templates when possible.
- Use `integration_tests/test-experiment-config*.yaml` for test-only configs.
