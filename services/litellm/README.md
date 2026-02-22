# LiteLLM Service

LiteLLM provides an OpenAI-compatible proxy for CRSBench, enabling unified access to multiple LLM providers with built-in logging and cost tracking.

## Quick Start

### 1. Set API Keys

Create a `.env` file in the project root:

```bash
# Required
CRSBENCH_LLM_MASTER_KEY=your-master-key-here

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

### Proxy Mode

LiteLLM can forward requests to another LiteLLM instance (upstream proxy). This is useful for:

- **Multi-tier setups**: Trial-level proxies forward to centralized proxy
- **Cost tracking**: Centralized proxy tracks all LLM usage and costs
- **Rate limiting**: Apply organization-wide rate limits
- **Model routing**: Centralized proxy handles complex routing logic

**Enable proxy mode:**

1. Set environment variables:
```bash
# Master key passed to CRS containers (for trial LiteLLM authentication)
CRSBENCH_LLM_MASTER_KEY=sk-trial-master-key

# Proxy configuration
CRSBENCH_LLM_UPSTREAM_BASE_URL=http://central-litellm:4000
CRSBENCH_LLM_API_KEY=sk-central-master-key  # Central LiteLLM's master key
```

2. Use the proxy mode config:
```bash
python scripts/litellm-helper.py start --config services/litellm/proxy-mode.yaml
```

**Architecture:**
```
CRS Container → Trial LiteLLM (Proxy) → Central LiteLLM → LLM Providers
   (uses          (forwards with           (connects to
CRSBENCH_LLM_MASTER_KEY) CRSBENCH_LLM_API_KEY)        providers)
                    ↓ logs                  ↓ logs
              trial-logs/              central-logs/
```

**Key Configuration:**
- **CRS containers**: Use `CRSBENCH_LLM_MASTER_KEY` to authenticate with trial LiteLLM
- **Trial LiteLLM**: Uses `CRSBENCH_LLM_API_KEY` to authenticate with central LiteLLM
- **Central LiteLLM**: Connects to providers with provider API keys

**Benefits:**
- Trial-level logging for debugging
- Central billing and cost tracking
- Simplified API key management (only central instance needs provider keys)
- Fine-grained access control per trial

**Sync models from upstream:**

Use the sync script to automatically fetch and configure models from upstream.
The script automatically loads environment variables from `.env` file.

```bash
# List available models from upstream (uses .env settings)
python scripts/sync-upstream-models.py --list-only

# Sync models to default-models.yaml (uses .env settings)
python scripts/sync-upstream-models.py

# Override with command line arguments
python scripts/sync-upstream-models.py \
  --upstream-url http://central-litellm:4000 \
  --api-key sk-central-master-key

# Skip confirmation prompt
python scripts/sync-upstream-models.py -y
```

**Manual configuration:**

Edit your config YAML to add proxy models:

```yaml
model_list:
  # Forward specific models to upstream
  - model_name: gpt-4o
    litellm_params:
      model: litellm_proxy/gpt-4o
      api_base: os.environ/CRSBENCH_LLM_UPSTREAM_BASE_URL
      api_key: os.environ/CRSBENCH_LLM_API_KEY

  - model_name: claude-sonnet-4
    litellm_params:
      model: litellm_proxy/claude-sonnet-4
      api_base: os.environ/CRSBENCH_LLM_UPSTREAM_BASE_URL
      api_key: os.environ/CRSBENCH_LLM_API_KEY
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

## Helper Scripts

### litellm-helper.py

The `scripts/litellm-helper.py` script provides convenient management of LiteLLM instances:

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

### sync-upstream-models.py

The `scripts/sync-upstream-models.py` script syncs model configurations from an upstream LiteLLM instance:

**List upstream models:**
```bash
# Uses .env file settings
python scripts/sync-upstream-models.py --list-only

# Or override with command line
python scripts/sync-upstream-models.py --list-only \
  --upstream-url http://central-litellm:4000 \
  --api-key sk-central-master-key
```

**Sync models with confirmation:**
```bash
# Uses .env file settings (CRSBENCH_LLM_UPSTREAM_BASE_URL and CRSBENCH_LLM_API_KEY)
python scripts/sync-upstream-models.py

# Skip confirmation prompt
python scripts/sync-upstream-models.py -y
```

**Custom output file:**
```bash
python scripts/sync-upstream-models.py \
  --output services/litellm/proxy-models.yaml
```

The script will:
1. Fetch all available models from upstream LiteLLM
2. Display models in a formatted list
3. Ask for confirmation before overwriting (unless `-y` flag)
4. Backup existing config to `.backup` file
5. Generate new proxy configuration with all upstream models

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

## Testing

### Quick Test (No API Calls)

Test LiteLLM with mock responses (no real API costs):

```bash
python scripts/test_litellm.py --port 4000 --mock-only
```

This tests:
- Health endpoint
- Models listing
- Mock completions (no API calls)
- Streaming responses

### Full Test (With Real API Calls)

⚠️ **Warning: This makes real API calls and may incur costs!**

```bash
python scripts/test_litellm.py --port 4000
```

Tests all features including real LLM API calls.

### Manual Testing with cURL

**Health check:**
```bash
curl http://localhost:4000/health
```

**List models:**
```bash
curl -X GET http://localhost:4000/models \
  -H "Authorization: Bearer $CRSBENCH_LLM_MASTER_KEY"
```

**Mock completion (no API call):**
```bash
curl -X POST http://localhost:4000/chat/completions \
  -H "Authorization: Bearer $CRSBENCH_LLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Hello"}],
    "mock_response": "Hello! This is a mock response."
  }'
```

**Real completion (makes API call):**
```bash
curl -X POST http://localhost:4000/chat/completions \
  -H "Authorization: Bearer $CRSBENCH_LLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

### Testing with Python

```python
import openai

# Configure client
client = openai.OpenAI(
    base_url="http://localhost:4000",
    api_key="your-master-key"
)

# Test with mock response (no API call)
response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "test"}],
    extra_body={"mock_response": "Test successful!"}
)
print(response.choices[0].message.content)

# Test with real API call
response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)
```

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
  -H "Authorization: Bearer $CRSBENCH_LLM_MASTER_KEY"
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
