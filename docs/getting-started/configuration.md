# Configuration

Use this page to get a CRSBench install into a runnable state.
See [Deployment](./deployment.md) for deployment topologies and [Environment Variables Reference](../reference/environment-variables.md) for the full variable index.

## First-Run Setup

1. Copy `.env.example`:

   ```bash
   cp .env.example .env
   ```

2. If the CRS needs LiteLLM, add the upstream endpoint and credential to `.env`:

   ```bash
   LITELLM_UPSTREAM_BASE_URL=http://your-litellm:4000
   LITELLM_UPSTREAM_API_KEY=sk-your-api-key
   # Required only for external mode with runtime.litellm.tracking_enabled: true
   # CRSBENCH_LLM_UPSTREAM_MASTER_KEY=sk-your-master-key
   ```

   Internal mode uses these variables from its model-routing file, while external mode uses them as the CRS-facing endpoint and credential.
   This step is not needed when the experiment sets `runtime.litellm.skip: true`.

3. Prepare OSS-Fuzz and base images:

   ```bash
   uv run crsbench prepare
   ```

4. Start Valkey/Redis:

   ```bash
   uv run python scripts/valkey-helper.py start
   ```

   For multi-machine setups, use `--password start` instead and see [Deployment](./deployment.md).

5. Validate the dependencies you actually use:

   ```bash
   uv run python scripts/valkey-helper.py status
   uv run python scripts/test_litellm.py --mock-only   # if LiteLLM is used
   ```

## LiteLLM Runtime Contract

CRSBench supports trial-scoped `internal` LiteLLM and an existing `external` LiteLLM endpoint.
Set `runtime.litellm.skip: true` for a CRS that does not use an LLM.

### Internal mode

Internal mode asks OSS-CRS to start one LiteLLM proxy for the trial and loads its routes from `crs_compose.litellm_config_path`.

```yaml
runtime:
  litellm:
    mode: internal
    tracking_enabled: true
    cost_budget: 30

crs_compose:
  litellm_config_path: configs/litellm-config.yaml
  crs-bug-finding-claude-code:
    num_cores: 8
```

The LiteLLM file maps every model alias required by the selected CRS to an upstream model.

```yaml
model_list:
  - model_name: claude-opus-4-8
    litellm_params:
      model: openai/gpt-5.5
      api_base: os.environ/LITELLM_UPSTREAM_BASE_URL
      api_key: os.environ/LITELLM_UPSTREAM_API_KEY
    model_info:
      input_cost_per_token: 0.000005
      cache_read_input_token_cost: 0.0000005
      output_cost_per_token: 0.00003
```

Each `os.environ/NAME` reference in the LiteLLM file must resolve in the trial worker environment.
CRSBench snapshots the LiteLLM file for local and distributed trials, transports it to managed GCE orchestrators and RQ workers, and keeps credential values in the worker environment.
When `tracking_enabled` is true, internal mode records the cumulative cost reported by OSS-CRS in the trial `llm-usage.json`; token, request, and per-model metrics are not available from this report.
An internal `cost_budget` must be a positive whole-dollar value because OSS-CRS applies it to each trial-scoped key.

### External mode

External mode connects each CRS directly to an existing LiteLLM-compatible endpoint.

```yaml
runtime:
  litellm:
    mode: external
    tracking_enabled: true
    cost_budget: 10.0
```

Set `LITELLM_UPSTREAM_BASE_URL` and either `LITELLM_UPSTREAM_API_KEY` or `CRSBENCH_LLM_UPSTREAM_MASTER_KEY`.
Use the LiteLLM proxy root for `LITELLM_UPSTREAM_BASE_URL` because tracking calls management endpoints such as `/key/info`.
When `tracking_enabled` is true, `CRSBENCH_LLM_UPSTREAM_MASTER_KEY` is required so CRSBench can create and inspect a per-trial virtual key.
See the upstream [LiteLLM documentation](https://docs.litellm.ai/) for provider routing, aliases, fallbacks, and key management.

## Troubleshooting

- `.env` is loaded from the repository root, so confirm the file location when runtime credentials are missing.
- Check Valkey connectivity:

  ```bash
  uv run python scripts/valkey-helper.py status
  ```

- Check LiteLLM reachability and credentials separately:

  ```bash
  uv run python scripts/test_litellm.py --mock-only
  ```

## What's Next

- Run a first experiment: [first-experiment.md](./first-experiment.md)
- Pick a workflow: [experiments.md](./experiments.md)
- Scale beyond one machine: [deployment.md](./deployment.md)
