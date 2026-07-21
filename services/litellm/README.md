# LiteLLM Routing

This directory contains LiteLLM routing examples and helper assets for CRSBench.
CRSBench supports a trial-scoped internal proxy managed by OSS-CRS and an external LiteLLM-compatible endpoint managed outside the trial.

## Internal Mode

Internal mode is the preferred way to expose CRS-required model aliases while routing requests to a different OpenAI-compatible model.
Create a LiteLLM YAML file whose `model_name` values cover the aliases declared by the selected CRS metadata.

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

Configure the experiment to pass that file to OSS-CRS.

```yaml
runtime:
  litellm:
    mode: internal
    tracking_enabled: true
    cost_budget: 30

crs_compose:
  litellm_config_path: /path/to/litellm-config.yaml
  crs-bug-finding-claude-code:
    num_cores: 8
```

Set `LITELLM_UPSTREAM_BASE_URL` and `LITELLM_UPSTREAM_API_KEY` in every worker environment referenced by the routing file.
The internal budget must be a positive whole-dollar value.
OSS-CRS starts and stops the proxy with the trial, and CRSBench writes the reported cumulative cost to `llm-usage.json`.
Internal spend reports do not contain token, request, or per-model usage metrics.

For RQ workers, CRSBench snapshots the routing YAML when submitting the experiment and stages a private copy for each trial.
For managed GCE runs, CRSBench transports the snapshot to the remote orchestrator before adding it to RQ trial payloads.
Keep credentials in worker environment layers and use `os.environ/NAME` references in the routing file.

## External Mode

External mode connects each CRS directly to an existing LiteLLM-compatible endpoint.

```yaml
runtime:
  litellm:
    mode: external
    tracking_enabled: true
    cost_budget: 10.0
```

Set `LITELLM_UPSTREAM_BASE_URL` and either `LITELLM_UPSTREAM_API_KEY` or `CRSBENCH_LLM_UPSTREAM_MASTER_KEY`.
Use the LiteLLM proxy root rather than a provider `/v1` path when external tracking is enabled.
When tracking is enabled, `CRSBENCH_LLM_UPSTREAM_MASTER_KEY` is required so CRSBench can create and inspect a per-trial virtual key.

## Standalone Proxy Helper

The helper starts a standalone LiteLLM container from a routing file and loads environment variables from the repository `.env` file.

```bash
uv run python scripts/litellm-helper.py start \
  --port 4000 \
  --config /path/to/litellm-config.yaml
uv run python scripts/litellm-helper.py health
uv run python scripts/litellm-helper.py logs -f
uv run python scripts/litellm-helper.py stop
```

This helper is separate from internal mode, where OSS-CRS owns the trial proxy lifecycle.

## Upstream Model Synchronization

The synchronization helper reads `LITELLM_UPSTREAM_BASE_URL` and `LITELLM_UPSTREAM_API_KEY`, lists models exposed by the upstream endpoint, and writes explicit proxy routes to `services/litellm/default-models.yaml`.

```bash
uv run python scripts/sync-upstream-models.py --list-only
uv run python scripts/sync-upstream-models.py
uv run python scripts/sync-upstream-models.py -y
```

Review the generated aliases before using the file with internal mode because every alias required by the selected CRS must be present.

## Validation

The mock-only check exercises a running standalone proxy without making a provider request.

```bash
uv run python scripts/test_litellm.py --port 4000 --mock-only
```

Running the same command without `--mock-only` sends real provider requests and can incur cost.
