# First Experiment

Use this page for the smallest happy path on one machine.

This page assumes the queue-backed runtime on a single host:
- one terminal runs `uv run crsbench run`
- one other terminal runs `uv run crsbench worker`
- `uv run crsbench evaluator` is not part of the minimal first-run path

If you want the fuller queue topology, CPU partitioning, or real-time
build/verify processing, use [Single-Machine Experiments](../deployment/single-machine.md)
or [Distributed Experiments](../deployment/distributed.md).

## 1. Start Services

```bash
uv run python scripts/valkey-helper.py start
```

If your CRS needs LiteLLM, configure `.env` first using
[configuration.md](./configuration.md).

## 2. Download the Sanity Suite

The public dataset is gated. Accept the Data Use Agreement for
`sslab-gatech/crsbench-dataset` at
<https://huggingface.co/datasets/sslab-gatech/crsbench-dataset>, then
authenticate (either set `HF_TOKEN=hf_...` in `.env`, or run `hf auth login`)
and download the small suite:

```bash
uv run hf auth login   # or set HF_TOKEN in .env
uv run crsbench download --benchmark-suite smoke/sanity
```

## 3. Pick a Config

For a first local run, use the bundled
[`experiment-configs/smoke-testing/first-run.yaml`](../../experiment-configs/smoke-testing/first-run.yaml).
It targets the `smoke/sanity` suite with the bundled
`atlantis-multilang-given_fuzzer` CRS, runs 3 trial jobs in parallel, and
needs no external LLM credentials (`runtime.litellm.skip: true`).

Expected local resources: Linux, Docker, 4 or more CPU cores, enough disk for
Docker images plus the sanity benchmark data, and a run window on the order of
minutes after the initial image pulls.

If you want a fuller starting point, use:
- [Distributed experiment config example](../experiment-config-distributed-example.yaml)
- [Example configs index](../reference/example-configs.md)

## 4. Start a Worker

In a separate terminal, start at least one worker before submitting the run.
The config sets `worker.jobs: 3`, so this single command spawns 3 worker
processes that pick up the 3 trial jobs in parallel:

```bash
uv run crsbench worker --experiment-config experiment-configs/smoke-testing/first-run.yaml
```

## 5. Submit the Experiment

```bash
uv run crsbench run --experiment-config experiment-configs/smoke-testing/first-run.yaml
```

`uv run crsbench run` submits work to Valkey and waits for worker-completed results.
If no worker is running, the submitter will enqueue jobs but no trial will
progress.

Do not start `uv run crsbench evaluator` for this first-run path. The evaluator
is for build/verify queues and benchmark-CI-style workflows, not the minimal
CRS trial queue.

## 6. Go Deeper

- Single-machine workflow details: [../deployment/single-machine.md](../deployment/single-machine.md)
- Distributed workflow: [../deployment/distributed.md](../deployment/distributed.md)
- Queue cleanup and recovery: [../operations/queue-and-recovery.md](../operations/queue-and-recovery.md)
