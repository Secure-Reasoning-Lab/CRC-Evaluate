# LiteLLM Service Design

## Overview

LiteLLM acts as an OpenAI-compatible proxy for CRSBench, providing:
- Unified API for multiple LLM providers (OpenAI, Anthropic, Google, Azure, etc.)
- Per-trial instances for isolation
- File-based request/response logging for snapshotting
- Dynamic configuration from environment variables and config files

## Architecture

### Direct Mode (Default)

```
┌─────────────────────────────────────────────────────────────┐
│                      CRSBench Trial                         │
│  ┌─────────────┐     ┌─────────────┐     ┌──────────────┐  │
│  │   CRS       │────▶│   LiteLLM   │────▶│ LLM Provider │  │
│  │  Container  │     │   Proxy     │     │ (OpenAI/etc) │  │
│  └─────────────┘     └─────────────┘     └──────────────┘  │
│                            │                                │
│                            ▼                                │
│                      ┌──────────┐                          │
│                      │  Logs    │                          │
│                      │  (JSON)  │                          │
│                      └──────────┘                          │
└─────────────────────────────────────────────────────────────┘
```

### Proxy Mode (Multi-tier)

```
┌────────────────────────────────────────────────────────────────────┐
│                         CRSBench Trial                             │
│  ┌───────────┐    ┌──────────────┐    ┌───────────────┐          │
│  │    CRS    │───▶│ Trial        │───▶│  Central      │──────────▶│
│  │ Container │    │ LiteLLM      │    │  LiteLLM      │  Provider │
│  └───────────┘    │ (Proxy)      │    │  (Gateway)    │           │
│       │           └──────────────┘    └───────────────┘          │
│  uses LITELLM_       │  forwards          │  uses provider        │
│  MASTER_KEY          │  with              │  API keys             │
│                      │  LITELLM_          │                        │
│                      │  API_KEY           │                        │
│                      ▼                     ▼                        │
│                   ┌──────────┐          ┌──────────┐              │
│                   │Trial Logs│          │Central   │              │
│                   │(per-trial│          │Logs      │              │
│                   │debugging)│          │(billing) │              │
│                   └──────────┘          └──────────┘              │
└────────────────────────────────────────────────────────────────────┘

Key Configuration:
- CRS containers: Use LITELLM_MASTER_KEY to authenticate with trial LiteLLM
- Trial LiteLLM: Uses LITELLM_API_KEY to authenticate with central LiteLLM
- Central LiteLLM: Connects to providers with provider API keys

Benefits:
- Trial-level logging for debugging
- Central cost tracking and billing
- Simplified API key management (only central instance needs provider keys)
- Fine-grained access control per trial
```

## Directory Structure

```
services/litellm/
├── docker-compose.yml.j2      # Jinja2 template for deployment
├── README.md                  # Usage documentation
└── default-models.yaml        # Default model configurations

scripts/
└── litellm-helper.py          # CLI for managing LiteLLM instances

Trial logs stored at:
  {experiment_filestore}/{experiment}/{crs}/{benchmark}/{trial_id}/litellm-logs/
```

## Configuration

### Environment Variables

API keys loaded from `.env` file (env vars take precedence over config):

| Variable | Purpose | Required |
|----------|----------|----------|
| `LITELLM_MASTER_KEY` | Authentication key passed to CRS containers | Yes |
| `LITELLM_API_KEY` | API key for authenticating with upstream LiteLLM (proxy mode only) | Proxy mode only |
| `UPSTREAM_LITELLM_BASE_URL` | URL of central/upstream LiteLLM instance (proxy mode only) | Proxy mode only |
| `OPENAI_API_KEY` | OpenAI API access | No (direct mode only) |
| `ANTHROPIC_API_KEY` | Anthropic API access | No (direct mode only) |
| `GOOGLE_API_KEY` | Google AI API access | No (direct mode only) |
| `AZURE_API_KEY` | Azure OpenAI access | No (direct mode only) |
| `AZURE_API_BASE` | Azure OpenAI endpoint | No (direct mode only) |
| `MISTRAL_API_KEY` | Mistral API access | No (direct mode only) |
| `GROQ_API_KEY` | Groq API access | No (direct mode only) |
| `TOGETHER_API_KEY` | Together AI API access | No (direct mode only) |
| `DEEPSEEK_API_KEY` | DeepSeek API access | No (direct mode only) |

### Model Configuration

Default models defined in `services/litellm/default-models.yaml`:

```yaml
model_list:
  # OpenAI
  - model_name: gpt-4o
    litellm_params:
      model: gpt-4o
      api_key: os.environ/OPENAI_API_KEY
  - model_name: gpt-4o-mini
    litellm_params:
      model: gpt-4o-mini
      api_key: os.environ/OPENAI_API_KEY

  # Anthropic
  - model_name: claude-sonnet-4-20250514
    litellm_params:
      model: claude-sonnet-4-20250514
      api_key: os.environ/ANTHROPIC_API_KEY

  # Google
  - model_name: gemini-2.0-flash
    litellm_params:
      model: gemini/gemini-2.0-flash
      api_key: os.environ/GOOGLE_API_KEY
```

## File-Based Logging

### Log Format

Each request/response logged as JSON file:

```
litellm-logs/
├── 2024-01-15T10:30:45.123_abc123.json
├── 2024-01-15T10:31:02.456_def456.json
└── ...
```

### Log Entry Schema

```json
{
  "id": "chatcmpl-abc123",
  "timestamp": "2024-01-15T10:30:45.123Z",
  "model": "gpt-4o-mini",
  "request": {
    "messages": [...],
    "temperature": 0.7,
    "max_tokens": 1000
  },
  "response": {
    "choices": [...],
    "usage": {
      "prompt_tokens": 150,
      "completion_tokens": 200,
      "total_tokens": 350
    }
  },
  "latency_ms": 1234,
  "cost_usd": 0.00025
}
```

### LiteLLM Settings for Logging

```yaml
litellm_settings:
  json_logs: true
  log_responses: true
  store_model_in_db: false

  # Custom callback for file logging
  success_callback: ["langfuse"]  # or custom file logger
  failure_callback: ["langfuse"]
```

## Docker Compose Template

`services/litellm/docker-compose.yml.j2`:

```yaml
services:
  litellm:
    image: ghcr.io/berriai/litellm:main-stable
    container_name: litellm-{{ trial_id }}
    environment:
      - LITELLM_MASTER_KEY=${LITELLM_MASTER_KEY}
      - OPENAI_API_KEY=${OPENAI_API_KEY:-}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
      - GOOGLE_API_KEY=${GOOGLE_API_KEY:-}
      - AZURE_API_KEY=${AZURE_API_KEY:-}
      - AZURE_API_BASE=${AZURE_API_BASE:-}
      - MISTRAL_API_KEY=${MISTRAL_API_KEY:-}
      - GROQ_API_KEY=${GROQ_API_KEY:-}
      - TOGETHER_API_KEY=${TOGETHER_API_KEY:-}
      - DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY:-}
    ports:
      - "{{ port }}:4000"
    volumes:
      - {{ config_path }}:/app/config.yaml:ro
      - {{ logs_path }}:/logs
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:4000/health"]
      interval: 10s
      timeout: 5s
      retries: 3
    command: --config /app/config.yaml --detailed_debug
    networks:
      - {{ network_name }}

networks:
  {{ network_name }}:
    driver: bridge
```

## Helper Script Commands

`scripts/litellm-helper.py`:

| Command | Description |
|---------|-------------|
| `start [--port PORT] [--config CONFIG]` | Start LiteLLM instance |
| `stop` | Stop running instance |
| `status` | Show instance status |
| `logs [-f]` | View container logs |
| `health` | Check health endpoint |

## Integration with CRSBench

### Experiment Config

Add to experiment config schema:

```yaml
litellm:
  enabled: true
  port: 4000  # Base port (incremented per trial)
  models_config: services/litellm/default-models.yaml  # Optional override
```

### CRS Executor Integration

Executors pass LiteLLM URL to CRS containers:

```python
# In crs_patch_executor.py
litellm_url = f"http://host.docker.internal:{litellm_port}"
cmd.extend(["--litellm-base", litellm_url, "--litellm-key", litellm_key])
```

## Security Considerations

- Master key required for all API calls
- API keys stored in `.env` file (not committed)
- Ports not exposed externally by default
- Per-trial isolation prevents cross-contamination
