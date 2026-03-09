# First Experiment

Use this page for the happy path on one machine.

## 1. Start Services

```bash
python scripts/valkey-helper.py start
```

If your CRS needs LiteLLM, ensure `.env` is configured first using
[configuration.md](./configuration.md).

## 2. Pick a Config

Start from the grouped experiment contract:

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
