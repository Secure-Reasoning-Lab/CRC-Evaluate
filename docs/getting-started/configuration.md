# Configuration Guide

Use this page to get a CRSBench environment into a runnable state. It covers the
minimum first-run configuration only.

Canonical variable index: [Environment Variables Reference](../reference/environment-variables.md)

For full experiment-config structure, use:
- [Distributed Experiment Config Example](../experiment-config-distributed-example.yaml)
- [Experiment Config Reference](../guides/experiments/config-reference.md)
- [Distributed Experiments Guide](../guides/experiments/distributed.md)
- [GCE Cloud Orchestration Guide](../guides/experiments/gce-cloud-orchestration.md)

Managed cloud config uses a provider-neutral `cloud.*` layout. Today the only
implemented managed backend is GCE via `cloud.providers.gce`.

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

Optional queue-backed distributed notifications use Apprise environment
variables. Build URLs with https://appriseit.com/tools/url-builder/. Then
set `CRSBENCH_NOTIFY_APPRISE_URLS` and, if needed, `CRSBENCH_NOTIFY_APPRISE_TITLE`
or `CRSBENCH_NOTIFY_APPRISE_TAG`. CRSBench sends the notification after
successful distributed cleanup, and on orchestrator or cleanup failures only
when tracked jobs still exist. Delivery is best-effort: send failures are
logged and do not fail the run.

To verify the notification target before running an experiment:

```bash
uv run python scripts/test_notification.py --dry-run
uv run python scripts/test_notification.py
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

Current support status:
- supported: `runtime.litellm.mode: external`
- supported: `runtime.litellm.skip: true`
- planned, not implemented: `runtime.litellm.mode: self_hosted`

## When To Use More Advanced Docs

Use the advanced guides when you need:
- multi-machine worker/evaluator topology
- managed cloud orchestrator/worker/evaluator fleets on GCE
- centralized or upstream LiteLLM deployment patterns
- queue cleanup / retry / recovery behavior
- full experiment-config authoring rules

Canonical advanced entry points:
- [Distributed Experiments Guide](../guides/experiments/distributed.md)
- [GCE Cloud Orchestration Guide](../guides/experiments/gce-cloud-orchestration.md)
- [Local Cloud Rehearsal](../guides/experiments/local-cloud-rehearsal.md)
- [Experiment Config Reference](../guides/experiments/config-reference.md)
- [Environment Variables Reference](../reference/environment-variables.md)

## Deployment Scenarios

### Local Development

Use this when you are iterating on CRSBench itself or running a single host with
the smallest possible dependency surface.

```bash
cp .env.example .env
uv run crsbench prepare
uv run python scripts/valkey-helper.py start
uv run python scripts/valkey-helper.py status
```

If the chosen CRS needs LiteLLM, set:

```bash
CRSBENCH_LLM_UPSTREAM_BASE_URL=http://localhost:4000
CRSBENCH_LLM_UPSTREAM_API_KEY=sk-your-api-key
```

If `runtime.litellm.tracking_enabled: true`, also set:

```bash
CRSBENCH_LLM_UPSTREAM_MASTER_KEY=sk-your-master-key
```

### Single-Machine Distributed

Use this when the orchestrator, worker, and optional evaluator all share one
host but still use the queue-backed distributed model.

```bash
uv run python scripts/valkey-helper.py start
uv run crsbench worker --experiment-config config.yaml
uv run crsbench run --experiment-config config.yaml
```

Add an evaluator only when you want build/verify queues to drain in the same
run:

```bash
uv run crsbench evaluator --experiment-config config.yaml
```

### Multi-Machine Distributed

Use this when workers or evaluators run on different hosts.

On the orchestrator host:

```bash
uv run python scripts/valkey-helper.py --password start
scp .env user@worker-1:/path/to/CRSBench/.env
scp .env user@worker-2:/path/to/CRSBench/.env
```

Worker and evaluator hosts should receive the same Redis settings and then add
their own runtime secrets if their CRS needs LiteLLM.

### Centralized LiteLLM / Proxy Mode

Use this when LiteLLM is centrally managed and trial hosts should not carry
provider API keys.

Central LiteLLM host:

```bash
CRSBENCH_LLM_MASTER_KEY=sk-central-master-key
OPENAI_API_KEY=sk-org-openai-key
ANTHROPIC_API_KEY=sk-org-anthropic-key
GOOGLE_API_KEY=sk-org-google-key
```

Trial hosts:

```bash
CRSBENCH_LLM_UPSTREAM_BASE_URL=http://central-litellm.example.com:4000
# For runs without tracking:
CRSBENCH_LLM_UPSTREAM_API_KEY=sk-central-runtime-key
# For runs with runtime.litellm.tracking_enabled: true:
CRSBENCH_LLM_UPSTREAM_MASTER_KEY=sk-central-master-key
```

In this layout, provider keys stay on the central LiteLLM instance. Trial hosts
only need the upstream endpoint plus the upstream runtime or master key.

If your workflow still uses the upstream-model sync helper, run it from the
trial host checkout:

```bash
uv run python scripts/sync-upstream-models.py --list-only
uv run python scripts/sync-upstream-models.py
```

## Troubleshooting and Configuration Hygiene

- Verify that `.env` is loaded from the repository root before debugging missing
  runtime credentials.
- Prefer canonical `CRSBENCH_LLM_*` variables over older aliases.
- Keep provider keys off worker or trial machines when proxy mode is used.
- Check Valkey connectivity first:

```bash
uv run python scripts/valkey-helper.py status
```

- Check LiteLLM reachability and credentials separately:

```bash
uv run python scripts/test_litellm.py --mock-only
```

- If source preparation or migration flows clone external repositories, set
  `PROJECT_REPOS_DIR` explicitly when you need those clones to live outside the
  default `.crsbench-repos/` cache directory.
