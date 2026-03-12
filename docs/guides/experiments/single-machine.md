# Single-Machine Experiments

This guide covers one-host operation when the orchestrator and worker run on the
same machine. The evaluator is optional and should be added only when you want
real-time build/verify processing in the same run environment.

## Minimal Normal Flow

```bash
uv run python scripts/valkey-helper.py start
uv run crsbench worker --experiment-config config.yaml
uv run crsbench run --experiment-config config.yaml
```

Notes:
- `uv run crsbench run` submits and monitors trial jobs.
- `uv run crsbench worker` is required for trial progress.
- `uv run crsbench evaluator` is not required for the minimal CRS experiment path.

## Optional Evaluator

Add an evaluator when you want build/verify queue processing on the same host,
for example when validating discovered artifacts in real time or when sharing a
host with benchmark-CI-style workflows.

```bash
uv run crsbench evaluator --experiment-config config.yaml --jobs 4 --cores-per-job 4
```

## CPU Allocation

Use CLI cpuset controls on worker and evaluator processes to partition host CPU
resources explicitly. Keep experiment YAML focused on per-trial resource needs
and CRS service sizing.

## Canonical References

- Full operational workflow: [distributed.md](./distributed.md)
- Queue behavior and retries: [queue-and-recovery.md](./queue-and-recovery.md)
- Config contract: [config-reference.md](./config-reference.md)
