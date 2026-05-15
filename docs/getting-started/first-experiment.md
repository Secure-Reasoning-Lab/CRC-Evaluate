# First Experiment

Use this page for the smallest happy path on one machine.

This page assumes the queue-backed runtime on a single host:
- one terminal runs `uv run crsbench run`
- one other terminal runs `uv run crsbench worker`
- `uv run crsbench evaluator` is not part of the minimal first-run path

If you want the fuller queue topology, CPU partitioning, or real-time
build/verify processing, use [Single-Machine Experiments](../guides/experiments/single-machine.md)
or [Distributed Experiments](../guides/experiments/distributed.md).

## 1. Start Services

```bash
uv run python scripts/valkey-helper.py start
```

If your CRS needs LiteLLM, configure `.env` first using
[configuration.md](./configuration.md).

## 2. Download the Sanity Suite

The public dataset is gated. Accept the Data Use Agreement for
`sslab-gatech/crsbench-dataset` at
<https://huggingface.co/datasets/sslab-gatech/crsbench-dataset>, then log in
and download the small suite:

```bash
uv run hf auth login
uv run crsbench download --benchmark-suite sanity
```

## 3. Pick a Config

For a first local run, create a small config like this:

Expected local resources: Linux, Docker, 4 or more CPU cores, enough disk for
Docker images plus the sanity benchmark data, and a run window on the order of
minutes after the initial image pulls.

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
  litellm:
    skip: true

storage:
  experiment_filestore: ./results/experiment-data
  report_filestore: ./results/report-data

crs_compose:
  atlantis-multilang-given_fuzzer:
    num_cores: 4
```

`atlantis-multilang-given_fuzzer` selects the bundled Atlantis multi-language
given-fuzzer CRS adapter. With `runtime.litellm.skip: true`, this starter run
does not require external LLM credentials.

If you want a fuller starting point, use:
- [Distributed experiment config example](../experiment-config-distributed-example.yaml)
- [Example configs index](../reference/example-configs.md)

## 4. Start a Worker

In a separate terminal, start at least one worker before submitting the run:

```bash
uv run crsbench worker --experiment-config first-run.yaml
```

## 5. Submit the Experiment

```bash
uv run crsbench run --experiment-config first-run.yaml
```

`uv run crsbench run` submits work to Valkey and waits for worker-completed results.
If no worker is running, the submitter will enqueue jobs but no trial will
progress.

Do not start `uv run crsbench evaluator` for this first-run path. The evaluator
is for build/verify queues and benchmark-CI-style workflows, not the minimal
CRS trial queue.

## 6. Go Deeper

- Single-machine workflow details: [../guides/experiments/single-machine.md](../guides/experiments/single-machine.md)
- Distributed workflow: [../guides/experiments/distributed.md](../guides/experiments/distributed.md)
- Queue cleanup and recovery: [../guides/experiments/queue-and-recovery.md](../guides/experiments/queue-and-recovery.md)
