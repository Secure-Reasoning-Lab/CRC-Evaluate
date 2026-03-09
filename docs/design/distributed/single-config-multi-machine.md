# Single Config Multi-Machine Contract

## Goal

Operate one experiment with a single config file across multiple machines:

- Machine A: orchestrator + Redis/Valkey
- Machine B: single evaluator
- Machine C..N: workers

## Runtime Model

- Orchestrator is the only process that loads experiment YAML.
- Orchestrator publishes runtime registration to Redis.
- Worker/evaluator start in configless mode and discover runtime registration from Redis.
- Queue model must be pinned cluster-wide via `CRSBENCH_QUEUE_MODEL`.

## Recommended Startup

Machine A:

```bash
export CRSBENCH_REDIS_HOST=<redis-host>:6379
export CRSBENCH_QUEUE_MODEL=flat
uv run crsbench run --experiment-config /path/to/config.yaml
```

Machine B (single evaluator):

```bash
export CRSBENCH_REDIS_HOST=<redis-host>:6379
export CRSBENCH_QUEUE_MODEL=flat
uv run crsbench evaluator
```

Machine C..N (workers):

```bash
export CRSBENCH_REDIS_HOST=<redis-host>:6379
export CRSBENCH_QUEUE_MODEL=flat
uv run crsbench worker --continuous
```

## Evaluator Capacity Policy

Default policy should be unified:

- `evaluator.jobs`
- `evaluator.cores_per_job`

Use the same capacity profile for both build and verify by default.
Only use split build/verify knobs for explicit advanced tuning.

## Storage Contract

Canonical run root:

```text
results/<experiment>/<run_id>/
  experiment-data/
  report-data/
  manifests/
```

Rules:

- Do not encode hostname into canonical path.
- Hostname should be metadata only.
- Trial copy/upload completion should be marker + manifest based.

## Recovery Contract

Per trial lifecycle:

```text
queued -> running -> produced -> replicated -> finalized
```

Recovery actions:

- produced but not replicated: retry copy/upload
- replicated but checksum mismatch: quarantine + recopy
- missing terminal state after timeout: requeue

## Machine-Specific Differences

Keep experiment config machine-agnostic.
Use host-local env/CLI for machine-specific tuning unless/until host-overrides are introduced.
