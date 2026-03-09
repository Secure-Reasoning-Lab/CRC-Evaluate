# Experiment Config Reference

The grouped experiment YAML contract is documented in:

- [Distributed experiment config example](../../experiment-config-distributed-example.yaml)

Use that file as the configuration source of truth for field layout, comments,
and examples.

## Core Contract

- `experiment`: task, mode, suite/benchmarks, sanitizers
- `runtime`: trials, timeouts, Redis host, LiteLLM settings, inputs
- `storage`: experiment/report/result storage paths
- `crs_compose`: CRS services and per-CRS runtime resources
- `worker` and `evaluator`: machine-local execution defaults
- `resources`: fallback per-trial resource defaults

## Input Contract

`runtime.inputs` is presence-based:

- `pov`
- `sarif`
- `seed`
- `diff`

If a key is absent, it is disabled. If present, it is enabled and validated
according to its fields.

## Related

- Distributed workflow: [distributed.md](./distributed.md)
- First experiment: [../../getting-started/first-experiment.md](../../getting-started/first-experiment.md)
