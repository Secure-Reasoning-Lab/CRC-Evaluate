# Single-Machine Experiments

This guide covers the normal local workflow where the orchestrator, worker, and
optional evaluator run on the same machine.

## Minimal Flow

```bash
python scripts/valkey-helper.py start
crsbench run --experiment-config config.yaml
crsbench worker --experiment-config config.yaml --continuous
crsbench evaluator --experiment-config config.yaml
```

Notes:
- `crsbench run` submits and monitors jobs.
- `crsbench worker` consumes CRS trial jobs.
- `crsbench evaluator` is optional but recommended for build/verify processing.

## CPU Allocation

Use CLI cpuset controls on worker and evaluator processes to partition host CPU
resources explicitly. Keep experiment YAML focused on per-trial resource needs
and CRS service sizing.

## Canonical References

- Full operational workflow: [distributed.md](./distributed.md)
- Queue behavior and retries: [queue-and-recovery.md](./queue-and-recovery.md)
- Config contract: [config-reference.md](./config-reference.md)
