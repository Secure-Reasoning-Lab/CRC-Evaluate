# First Experiment

Use this page for the happy path on one machine.

## 1. Start Services

```bash
python scripts/valkey-helper.py start
```

If your CRS needs LiteLLM, ensure `.env` is configured first using
[configuration.md](./configuration.md).

## 2. Pick a Config

For a first local run, create a small config like this:

```yaml
experiment:
  name: first-run
  task: bugfinding
  mode: full
  benchmark_suite: sanity
  sanitizers: [address]

runtime:
  trials: 1
  max_total_time: 3600
  build_timeout: 900
  run_timeout: 1800
  verify_timeout: 900
  redis_host: localhost:6379

storage:
  experiment_filestore: ./results/experiment-data
  report_filestore: ./results/report-data

skip_litellm: true

crs_compose:
  atlantis-multilang-given_fuzzer:
    num_cores: 4
```

If you want a fuller starting point, use:

- [Distributed experiment config example](../experiment-config-distributed-example.yaml)
- [Example configs index](../reference/example-configs.md)

## 3. Run the Experiment

```bash
crsbench run --experiment-config path/to/config.yaml
crsbench worker --experiment-config path/to/config.yaml --continuous
```

Optionally start an evaluator in another terminal if you want build/verify work
to be processed immediately:

```bash
crsbench evaluator --experiment-config path/to/config.yaml
```

## 4. Go Deeper

- Single-machine workflow details: [../guides/experiments/single-machine.md](../guides/experiments/single-machine.md)
- Distributed workflow: [../guides/experiments/distributed.md](../guides/experiments/distributed.md)
- Queue cleanup and recovery: [../guides/experiments/queue-and-recovery.md](../guides/experiments/queue-and-recovery.md)
