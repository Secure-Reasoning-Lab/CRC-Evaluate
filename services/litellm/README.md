# LiteLLM Service

LiteLLM provides an OpenAI-compatible proxy for CRSBench, enabling unified access to multiple LLM providers with built-in logging and cost tracking.

## Quick Start

### 1. Set API Keys

Create a `.env` file in the project root:

```bash
# Required
LITELLM_MASTER_KEY=your-master-key-here

# Provider API Keys (at least one required)
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=...
# Add other providers as needed
```

### 2. Start LiteLLM

Using the helper script:

```bash
python scripts/litellm-helper.py start --port 4000
```

Or manually with docker-compose:

```bash
cd services/litellm
docker-compose up -d
```

### 3. Test Connection

```bash
curl http://localhost:4000/health
```

### 4. Use in CRS

```python
import openai

client = openai.OpenAI(
    base_url="http://localhost:4000",
    api_key="your-master-key"
)

response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Hello!"}]
)
```

## Configuration

### Default Models

The `default-models.yaml` file defines available models. To customize:

1. Copy `default-models.yaml` to your trial directory
2. Edit the model list as needed
3. Mount the custom config in docker-compose

### Supported Providers

| Provider | Models | API Key Variable |
|----------|--------|------------------|
| OpenAI | gpt-4o, gpt-4o-mini, gpt-4-turbo | `OPENAI_API_KEY` |
| Anthropic | claude-sonnet-4, claude-3-5-sonnet, claude-3-5-haiku | `ANTHROPIC_API_KEY` |
| Google | gemini-2.0-flash, gemini-1.5-pro | `GOOGLE_API_KEY` |
| Mistral | mistral-large, mistral-small | `MISTRAL_API_KEY` |
| Groq | llama-3.3-70b-versatile | `GROQ_API_KEY` |
| Together AI | meta-llama-3.1-70b-instruct-turbo | `TOGETHER_API_KEY` |
| DeepSeek | deepseek-chat | `DEEPSEEK_API_KEY` |

### Azure OpenAI

For Azure OpenAI, set additional environment variables:

```bash
AZURE_API_KEY=your-azure-key
AZURE_API_BASE=https://your-resource.openai.azure.com
AZURE_API_VERSION=2024-02-15-preview
```

## Logging

LiteLLM logs all requests/responses as JSON files in the mounted logs directory.

### Log Location

When using with CRSBench trials:
```
{experiment_filestore}/{experiment}/{crs}/{benchmark}/{trial_id}/litellm-logs/
```

### Log Format

Each request creates a JSON file named `{timestamp}_{request_id}.json`:

```json
{
  "id": "chatcmpl-abc123",
  "timestamp": "2024-01-15T10:30:45.123Z",
  "model": "gpt-4o-mini",
  "request": {
    "messages": [...],
    "temperature": 0.7
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

## Helper Script

The `scripts/litellm-helper.py` script provides convenient management:

### Start Instance

```bash
python scripts/litellm-helper.py start --port 4000 --config default-models.yaml
```

### Stop Instance

```bash
python scripts/litellm-helper.py stop
```

### Check Status

```bash
python scripts/litellm-helper.py status
```

### View Logs

```bash
python scripts/litellm-helper.py logs
python scripts/litellm-helper.py logs -f  # follow logs
```

### Health Check

```bash
python scripts/litellm-helper.py health
```

## Integration with CRSBench

CRSBench automatically manages LiteLLM instances for each trial when enabled in the experiment configuration.

### Enable in Experiment Config

```yaml
litellm:
  enabled: true
  port: 4000  # Base port (incremented per trial)
  models_config: services/litellm/default-models.yaml
```

### Automatic Management

CRSBench will:
1. Start a LiteLLM instance for each trial on a unique port
2. Pass the LiteLLM URL to CRS executors via `--litellm-base`
3. Mount trial-specific logs directory
4. Clean up instances after trial completion

## Troubleshooting

### Container Won't Start

Check API keys are set:
```bash
docker logs litellm
```

### Models Not Available

Verify the API key for that provider is set and valid:
```bash
curl -X GET http://localhost:4000/models \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

### Port Already in Use

Change the port in docker-compose or use `--port` flag:
```bash
python scripts/litellm-helper.py start --port 4001
```

### High Costs

Monitor usage through logs:
```bash
# Sum total costs from logs
cat litellm-logs/*.json | jq '.cost_usd' | paste -sd+ | bc
```

## Security

- **Never commit `.env` files** - API keys should stay private
- **Master key required** - All requests must authenticate
- **Port binding** - By default, only bound to localhost
- **Per-trial isolation** - Each trial gets its own instance

## References

- [LiteLLM Documentation](https://docs.litellm.ai/)
- [Supported Providers](https://docs.litellm.ai/docs/providers)
- [LiteLLM Proxy](https://docs.litellm.ai/docs/proxy/quick_start)
