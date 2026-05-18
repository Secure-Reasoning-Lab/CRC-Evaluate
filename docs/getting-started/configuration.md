# Configuration

Use this page to get a CRSBench install into a runnable state. For deployment
topologies, see [deployment.md](./deployment.md). For the full variable index,
see [Environment Variables Reference](../reference/environment-variables.md).

## First-Run Setup

1. Copy `.env.example`:

   ```bash
   cp .env.example .env
   ```

2. If the CRS you will run needs LiteLLM, edit `.env`:

   ```bash
   CRSBENCH_LLM_UPSTREAM_BASE_URL=http://your-litellm:4000
   CRSBENCH_LLM_UPSTREAM_API_KEY=sk-your-api-key
   # Required only when runtime.litellm.tracking_enabled: true
   # CRSBENCH_LLM_UPSTREAM_MASTER_KEY=sk-your-master-key
   ```

   CRSBench currently does not manage LiteLLM for you. You need to stand up
   your own LiteLLM instance (for example via `scripts/litellm-helper.py`
   locally, or an externally-hosted proxy) and point the variables above at
   it. For provider keys, routing, fallbacks, and other LiteLLM-side
   settings, refer to the upstream [LiteLLM docs](https://docs.litellm.ai/).

   When the experiment config sets `runtime.litellm.skip: true`, this step is
   not needed.

3. Prepare OSS-Fuzz and base images:

   ```bash
   uv run crsbench prepare
   ```

4. Start Valkey/Redis:

   ```bash
   uv run python scripts/valkey-helper.py start
   ```

   For multi-machine setups, use `--password start` instead. See
   [deployment.md](./deployment.md).

5. Validate the dependencies you actually use:

   ```bash
   uv run python scripts/valkey-helper.py status
   uv run python scripts/test_litellm.py --mock-only   # if LiteLLM is used
   ```

## LiteLLM Runtime Contract

CRSBench expects an externally-managed LiteLLM endpoint and only owns the
client-side wiring documented here. For configuring the LiteLLM server
itself (providers, model aliases, routing, fallbacks, key management, cost
tracking), see the upstream [LiteLLM docs](https://docs.litellm.ai/).

CRSBench uses canonical `CRSBENCH_LLM_*` names.

Required when `runtime.litellm.mode: external`:

- `CRSBENCH_LLM_UPSTREAM_BASE_URL`
- one of `CRSBENCH_LLM_UPSTREAM_API_KEY` or `CRSBENCH_LLM_UPSTREAM_MASTER_KEY`

Additionally required when `runtime.litellm.tracking_enabled: true`:

- `CRSBENCH_LLM_UPSTREAM_MASTER_KEY`

Support status:

- supported: `runtime.litellm.mode: external`
- supported: `runtime.litellm.skip: true`
- planned, not implemented: `runtime.litellm.mode: self_hosted`

## Troubleshooting

- `.env` is loaded from the repository root. If runtime credentials look
  missing, confirm the file location first.
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
