# Environment Variables Reference

This page is the canonical index for CRSBench environment variables.

## Core Runtime

| Variable | Purpose |
|---|---|
| `CRSBENCH_REDIS_HOST` | Enables distributed mode and sets queue backend host (`host` or `host:port`; default port is `6379`). |
| `CRSBENCH_REDIS_PASSWORD` | Password for Redis/Valkey auth (if enabled). |
| `CRSBENCH_LLM_BASE_URL` | Immediate LiteLLM endpoint called directly. |
| `CRSBENCH_LLM_UPSTREAM_BASE_URL` | Upstream/forwarding LiteLLM endpoint. |
| `CRSBENCH_LLM_UPSTREAM_MASTER_KEY` | Upstream LiteLLM key-management/tracking credential (`external` mode preferred). |
| `CRSBENCH_LLM_UPSTREAM_API_KEY` | Upstream LiteLLM runtime API key (`external` mode preferred). |

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

- Setup and practical examples: `docs/environment-setup.md`
- Distributed workflow and CI notes: `docs/experiment-workflow.md`
- LiteLLM service details: `services/litellm/README.md`
- Valkey service details: `services/valkey/README.md`
- Template env file: `.env.example`

## Notes

- CRSBench loads environment variables from `.env` via `python-dotenv` in CLI/runtime entrypoints.
- Use `.env.example` as the canonical template and copy it to `.env` before running:
  `cp .env.example .env`
- CRSBench uses canonical `CRSBENCH_*` names for framework-owned runtime variables.
- Runtime controls (experiment/worker identity and log verbosity) are configured via config/CLI, not env vars.
