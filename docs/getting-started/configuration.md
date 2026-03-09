# Configuration Guide

Use this page to get a CRSBench environment into a runnable state. It covers the
minimum first-run configuration only.

Canonical variable index: [Environment Variables Reference](../reference/environment-variables.md)

For full experiment-config structure, use:
- [Distributed Experiment Config Example](../experiment-config-distributed-example.yaml)
- [Experiment Config Reference](../guides/experiments/config-reference.md)
- [Distributed Experiments Guide](../guides/experiments/distributed.md)

## First-Run Setup

1. Copy the example file:

```bash
cp .env.example .env
```

2. If your chosen CRS run needs LiteLLM, edit `.env` with the minimum runtime
contract:

```bash
CRSBENCH_LLM_UPSTREAM_BASE_URL=http://your-litellm:4000
CRSBENCH_LLM_UPSTREAM_API_KEY=sk-your-api-key
# Needed only when runtime.litellm.tracking_enabled: true
# CRSBENCH_LLM_UPSTREAM_MASTER_KEY=sk-your-master-key
```

If your experiment config sets `runtime.litellm.skip: true`, you can skip this
LiteLLM setup for that run.

3. Prepare the managed OSS-Fuzz checkout and required base images:

```bash
uv run crsbench prepare
```

4. If you will run the normal local first experiment, start Valkey/Redis:

```bash
uv run python scripts/valkey-helper.py start
```

If you will run multi-machine or remote-worker distributed experiments, use
password-protected startup instead:

```bash
uv run python scripts/valkey-helper.py --password start
```

5. Validate the local helper services you depend on:

```bash
uv run python scripts/valkey-helper.py status
```

If this run uses LiteLLM, validate that separately:

```bash
uv run python scripts/test_litellm.py --mock-only
```

## Minimum Runtime Contract

CRSBench uses canonical `CRSBENCH_LLM_*` names.

Required only when your run uses external LiteLLM:
- `CRSBENCH_LLM_UPSTREAM_BASE_URL`
- one of:
  - `CRSBENCH_LLM_UPSTREAM_API_KEY`
  - `CRSBENCH_LLM_UPSTREAM_MASTER_KEY`

Additionally required when `runtime.litellm.tracking_enabled: true`:
- `CRSBENCH_LLM_UPSTREAM_MASTER_KEY`

## When To Use More Advanced Docs

Use the advanced guides when you need:
- multi-machine worker/evaluator topology
- centralized or upstream LiteLLM deployment patterns
- queue cleanup / retry / recovery behavior
- full experiment-config authoring rules

Canonical advanced entry points:
- [Distributed Experiments Guide](../guides/experiments/distributed.md)
- [Experiment Config Reference](../guides/experiments/config-reference.md)
- [Environment Variables Reference](../reference/environment-variables.md)
