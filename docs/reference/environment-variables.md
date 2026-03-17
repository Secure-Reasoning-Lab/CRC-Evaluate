# Environment Variables Reference

This page is the canonical index for CRSBench environment variables.

## Core Runtime

| Variable | Purpose |
|---|---|
| `CRSBENCH_REDIS_HOST` | Sets the Redis/Valkey queue backend host (`host` or `host:port`; default port is `6379`) for queue-backed worker/evaluator/configless flows and as a runtime default where applicable. |
| `CRSBENCH_REDIS_PASSWORD` | Password for Redis/Valkey auth (if enabled). |
| `CRSBENCH_LLM_MASTER_KEY` | Local/self-hosted LiteLLM auth key used by CRS-facing trial services or centralized proxy deployments. |
| `CRSBENCH_LLM_BASE_URL` | Immediate LiteLLM endpoint called directly. |
| `CRSBENCH_LLM_UPSTREAM_BASE_URL` | Upstream/forwarding LiteLLM endpoint. |
| `CRSBENCH_LLM_UPSTREAM_MASTER_KEY` | Upstream LiteLLM key-management/tracking credential (`external` mode preferred). |
| `CRSBENCH_LLM_UPSTREAM_API_KEY` | Upstream LiteLLM runtime API key (`external` mode preferred). |
| `PROJECT_REPOS_DIR` | Optional cache directory for cloned upstream project repositories used by migration/source-preparation flows (default: `.crsbench-repos/`). |

## Cloud Startup Overrides (Advanced)

These variables are consumed by the cloud startup scripts and are primarily for
local rehearsal or non-GCE bring-up.

| Variable | Purpose |
|---|---|
| `CRSBENCH_METADATA_ROOT_DIR` | Read instance metadata from mounted files instead of an HTTP metadata service. |
| `CRSBENCH_METADATA_BASE_URL` | Override the metadata-service base URL when not using the default GCE endpoint. |
| `CRSBENCH_METADATA_HEADER_NAME` | Override the metadata-service header name (default `Metadata-Flavor`). |
| `CRSBENCH_METADATA_HEADER_VALUE` | Override the metadata-service header value (default `Google`). |
| `CRSBENCH_SERVICE_MANAGER` | Worker/orchestrator startup mode: `auto` (default), `systemd`, or `foreground`. |
| `CRSBENCH_USER` | Non-root account created by the startup scripts for checkout/install/runtime handoff (default `crsbench`). |
| `CRSBENCH_TIMEZONE` | Host timezone enforced during startup (default `America/New_York`). |
| `CRSBENCH_STATE_DIR` | Override the startup-script state directory (default `/var/lib/crsbench`). |
| `CRSBENCH_CLONE_DIR` | Override the checkout target directory used during startup (default `/opt/crsbench`). |

### LiteLLM External Mode Contract

`runtime.litellm.mode: external` is the supported experiment-runtime path.

`runtime.litellm.mode: self_hosted` is reserved but not implemented yet.

- When `runtime.litellm.tracking_enabled: true`, set:
  - `CRSBENCH_LLM_UPSTREAM_BASE_URL` (or `CRSBENCH_LLM_BASE_URL`)
  - `CRSBENCH_LLM_UPSTREAM_MASTER_KEY`
- When `runtime.litellm.tracking_enabled: false`, set:
  - `CRSBENCH_LLM_UPSTREAM_BASE_URL` (or `CRSBENCH_LLM_BASE_URL`)
  - one of `CRSBENCH_LLM_UPSTREAM_API_KEY` or `CRSBENCH_LLM_UPSTREAM_MASTER_KEY`

`CRSBENCH_LLM_MASTER_KEY` is for local or centrally managed LiteLLM auth
surfaces and is not the external-mode tracking control-plane key used by
CRSBench experiment runtime.

### Centralized LiteLLM / Proxy Mode

When a central LiteLLM instance fronts all provider accounts:

- the central LiteLLM host keeps provider keys such as `OPENAI_API_KEY`,
  `ANTHROPIC_API_KEY`, and `GOOGLE_API_KEY`
- trial or worker hosts set only:
  - `CRSBENCH_LLM_UPSTREAM_BASE_URL`
  - one of:
    - `CRSBENCH_LLM_UPSTREAM_API_KEY`
    - `CRSBENCH_LLM_UPSTREAM_MASTER_KEY`

That split keeps provider credentials off experiment runners while preserving
external-mode tracking and runtime auth.

## Evaluator Resource Propagation (Advanced)

These variables are set by supervisor/resource context for worker job isolation.
They are consumed by OSS-Fuzz helper Docker runs and CRSBench direct Docker runs.

| Variable | Purpose |
|---|---|
| `OSS_FUZZ_CPUSET_CPUS` | CPU pinning for per-job containers (for example `80-95`). |
| `OSS_FUZZ_CGROUP_PARENT` | Docker cgroup parent path for per-job cgroup enforcement. |
| `OSS_FUZZ_DOCKER_NETWORK` | Optional Docker network mode override for helper/direct `docker run` (for example `none`). |

## Runtime Controls (Non-env)

- Experiment identity and queue naming: `experiment` in `experiment-config.yaml`
- Worker identity: `worker.worker_name` in `experiment-config.yaml` or `--worker-name`
- Verbose logging: `--verbose` on CLI commands

## Provider Keys

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | Provider key for OpenAI models via LiteLLM. |
| `ANTHROPIC_API_KEY` | Provider key for Anthropic models via LiteLLM. |
| `GOOGLE_API_KEY` | Provider key for Google models via LiteLLM. |
| `AZURE_API_KEY` | Provider key for Azure OpenAI via LiteLLM. |
| `MISTRAL_API_KEY` | Provider key for Mistral via LiteLLM. |
| `GROQ_API_KEY` | Provider key for Groq via LiteLLM. |
| `TOGETHER_API_KEY` | Provider key for Together via LiteLLM. |
| `DEEPSEEK_API_KEY` | Provider key for DeepSeek via LiteLLM. |
| `COHERE_API_KEY` | Provider key for Cohere via LiteLLM. |

## Component-Specific References

- Setup and practical examples: [Getting Started Configuration](../getting-started/configuration.md)
- Distributed workflow and CI notes: [Distributed Experiments](../guides/experiments/distributed.md)
- LiteLLM service details: [`services/litellm/README.md`](../../services/litellm/README.md)
- Valkey service details: [`services/valkey/README.md`](../../services/valkey/README.md)
- Template env file: [`.env.example`](../../.env.example)

## Notes

- CRSBench loads environment variables from `.env` via `python-dotenv` in CLI/runtime entrypoints.
- Use `.env.example` as the canonical template and copy it to `.env` before running:
  `cp .env.example .env`
- CRSBench uses canonical `CRSBENCH_*` names for framework-owned runtime variables.
- Runtime controls (experiment/worker identity and log verbosity) are configured via config/CLI, not env vars.
- `PROJECT_REPOS_DIR` affects repository-cloning helper flows only; it does not
  change benchmark discovery or worker queue behavior.
