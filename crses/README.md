# CRS Registry and Configuration

This directory maintains the local CRS (Cyber Reasoning System) registry and deployment configurations for CRSBench.

## Overview

The `crses/` directory provides a local registry of CRS implementations and their deployment configurations, mirroring the structure of the upstream `oss-crs` package. This allows CRSBench to:

- Maintain custom CRS definitions
- Store deployment-specific configurations
- Keep secrets and credentials separate from code
- Support multi-worker CRS deployments

## Directory Structure

```
crses/
├── registry/                   # CRS package definitions (immutable templates)
│   └── <crs-name>/
│       ├── pkg.yaml            # Package metadata
│       └── config-crs.yaml     # CRS requirements
└── configs/                    # Deployment configurations (user-specific)
    └── <config-name>/
        ├── .env                # Environment variables (secrets)
        ├── .gitignore          # Git ignore patterns
        ├── config-crs.yaml     # CRS-specific configuration
        ├── config-litellm.yaml # LiteLLM model list
        ├── config-resource.yaml # Resource allocation
        └── config-worker.yaml  # Worker-specific overrides (optional)
```

## Registry Files (`registry/<crs-name>/`)

Registry entries define the CRS package template - immutable metadata about a CRS implementation.

### `pkg.yaml` - Package Metadata

Defines the CRS package name, type, and source repository.

**Format:**
```yaml
name: <crs-name>
type: bug-finding | bug-fixing
source:
  url: <git-repository-url>
  ref: <branch-or-tag>
```

**Example** (`registry/mock-crs/pkg.yaml`):
```yaml
name: mock-crs
type: bug-finding
source:
  url: https://github.com/Team-Atlanta/mock-crs
  ref: main
```

**Fields:**
- `name`: CRS identifier (must match directory name)
- `type`: Either `bug-finding` (vulnerability discovery) or `bug-fixing` (patch generation)
- `source.url`: Git repository URL
- `source.ref`: Branch, tag, or commit hash

### `config-crs.yaml` - CRS Requirements

Defines the CRS runtime requirements and dependencies.

**Format:**
```yaml
<crs-name>:
  models:              # LLM models required
    - <model-name>
  cpu: <cpu-spec>      # CPU allocation
  ram: <memory-spec>   # Memory allocation
  dependencies: []     # Runtime dependencies
  features: []         # Optional features
```

**Example** (`registry/mock-crs/config-crs.yaml`):
```yaml
mock-crs: []
```

**Fields:**
- `models`: List of LLM model names (e.g., `gpt-4o-mini`, `claude-sonnet-4`)
- `cpu`: CPU cores (`1`, `1-4`, `all`)
- `ram`: Memory limit (e.g., `4G`, `16G`)
- `dependencies`: External services or libraries
- `features`: CRS-specific feature flags

## Config Files (`configs/<config-name>/`)

Configuration entries define deployment-specific settings - how to run CRS instances with specific resources and credentials.

### `.env` - Environment Variables

Stores secrets and credentials (never commit to git).

**Format:**
```bash
POSTGRES_PASSWORD=<password>
POSTGRES_PORT=5432
LITELLM_MASTER_KEY=<api-key>
```

**Example** (`configs/mock-crs/.env`):
```bash
POSTGRES_PASSWORD=super_secure_password
POSTGRES_PORT=5432
LITELLM_MASTER_KEY=sk-1234
```

### `config-crs.yaml` - CRS Configuration

Mirrors the registry CRS config, can override defaults.

**Example** (`configs/mock-crs/config-crs.yaml`):
```yaml
mock-crs: []
```

### `config-litellm.yaml` - LiteLLM Model List

Defines available LLM models and their endpoints.

**Format:**
```yaml
model_list:
  - model_name: <name>
    litellm_params:
      model: <provider/model>
      api_key: <key>
      # Additional provider-specific params
```

**Example** (`configs/mock-crs/config-litellm.yaml`):
```yaml
model_list: []
```

### `config-resource.yaml` - Resource Allocation

Defines workers, resource limits, and CRS placement. This is the most important configuration file.

**Format:**
```yaml
workers:
  <worker-name>:
    cpuset: "<cpu-range>"     # CPU cores (e.g., "0-15")
    memory: "<size>"          # RAM limit (e.g., "64G")

crs:
  <crs-name>:
    workers:
      - <worker-name>
    resources:                # Optional: explicit resource allocation
      <worker-name>:
        cpu: <cores>
        memory: <size>
    llm:                      # Optional: LLM budget allocation
      max_budget: <dollars>
      max_rpm: <requests>
      max_tpm: <tokens>
```

**Example** (`configs/mock-crs/config-resource.yaml`):
```yaml
workers:
  local:
    cpuset: "0-15"
    memory: "64G"

crs:
  mock-crs:
    workers:
      - local
```

**Key Concepts:**

1. **Workers**: Physical or virtual machines available for deployment
2. **CRS Placement**: Which worker(s) run each CRS instance
3. **Resource Allocation**: CPU/memory limits per CRS
4. **LLM Budgets**: API cost controls (see Resource Allocation Guide below)

### `config-worker.yaml` - Worker Overrides (Optional)

Worker-specific configuration overrides. Not present in minimal setups.

## Relationship with oss-crs

The `crses/` directory structure mirrors the upstream `oss-crs` package:

| CRSBench (`crses/`) | oss-crs Package |
|---------------------|-----------------|
| `registry/` | `crs_registry/` (14 CRS entries) |
| `configs/` | `example_configs/` (9 deployment configs) |
| Local, minimal | Upstream, comprehensive |

**Key Differences:**

- **`crses/`**: Local CRSBench registry (currently stub with mock-crs only)
- **`oss-crs/crs_registry/`**: Full upstream registry with production CRS implementations:
  - Bug-finding: `atlantis-c-*`, `atlantis-java-*`, `crs-libfuzzer`, `mock-crs`
  - Bug-fixing: `42-patch-agent`, `buttercup-patcher`, `swe-agent`
- **`oss-crs/example_configs/`**: Production deployment configurations

**Usage Pattern:**
1. Reference CRS from `oss-crs/crs_registry/` (upstream)
2. Create deployment config in `crses/configs/` (local)
3. Or: Add custom CRS to `crses/registry/` (local development)

## Resource Allocation Guide

The `config-resource.yaml` format supports flexible resource allocation strategies.

### Minimal Configuration

```yaml
workers:
  local:
    cpuset: "0-7"
    memory: "32G"

crs:
  atlantis-c-libafl:
    workers: [local]
```

**Result**: CPU/memory auto-allocated from worker capacity.

### Explicit Resource Control

```yaml
workers:
  local:
    cpuset: "0-15"
    memory: "64G"

crs:
  atlantis-c-libafl:
    workers: [local]
    resources:
      local:
        cpu: 8
        memory: 16G
```

**Result**: CRS gets 8 cores and 16GB RAM.

### Multi-Worker Deployment

```yaml
workers:
  server1:
    cpuset: "0-31"
    memory: "128G"
  server2:
    cpuset: "0-31"
    memory: "128G"

crs:
  atlantis-c-libafl:
    workers: [server1, server2]
    resources:
      server1:
        cpu: 16
        memory: 64G
      server2:
        cpu: 16
        memory: 64G
```

**Result**: CRS instances run on both servers with specified resources.

### LLM Budget Management

```yaml
workers:
  local:
    cpuset: "0-15"
    memory: "64G"

llm:
  max_budget: 100      # Total budget for ALL CRSs
  max_rpm: 1200        # Total requests per minute
  max_tpm: 1000000     # Total tokens per minute

crs:
  crs1:
    workers: [local]
    llm:
      max_budget: 60   # This CRS gets $60
      max_rpm: 720
      max_tpm: 600000

  crs2:
    workers: [local]
    llm:
      max_budget: 40   # This CRS gets $40
      max_rpm: 480
      max_tpm: 400000
```

**Key Points:**
- Global `llm` section defines TOTAL budget for all CRSs
- Per-CRS `llm` sections allocate portions of global budget
- Sum of per-CRS allocations must not exceed global limits
- Unspecified CRSs get equal shares of remaining budget

For detailed resource configuration guide, see: `oss-crs/example_configs/*/RESOURCE_CONFIG_README.md`

## Usage

### Adding a New CRS Registry Entry

1. Create directory: `mkdir -p registry/<crs-name>`
2. Create `pkg.yaml` with package metadata
3. Create `config-crs.yaml` with requirements

### Creating a Deployment Configuration

1. Create directory: `mkdir -p configs/<config-name>`
2. Copy template files from `oss-crs/example_configs/reference/`
3. Edit `config-resource.yaml` for worker and resource allocation
4. Edit `config-litellm.yaml` for LLM model endpoints
5. Create `.env` with secrets (add to `.gitignore`)
6. Optionally customize `config-crs.yaml` and `config-worker.yaml`

### Using with crsbench

Reference configs by name when running experiments:

```bash
# Reference a config from crses/configs/
crsbench --crs-config configs/my-deployment ...

# Reference a CRS from crses/registry/
crsbench --crs registry/my-crs ...
```

## Examples

### Example 1: Mock CRS (Bug Finding)

**Registry** (`registry/mock-crs/pkg.yaml`):
```yaml
name: mock-crs
type: bug-finding
source:
  url: https://github.com/Team-Atlanta/mock-crs
  ref: main
```

**Config** (`configs/mock-crs/config-resource.yaml`):
```yaml
workers:
  local:
    cpuset: "0-15"
    memory: "64G"

crs:
  mock-crs:
    workers:
      - local
```

### Example 2: Multi-CRS Deployment

**Config** (`configs/ensemble-c/config-resource.yaml`):
```yaml
workers:
  local:
    cpuset: "0-31"
    memory: "128G"

llm:
  max_budget: 300
  max_rpm: 3000
  max_tpm: 3000000

crs:
  atlantis-c-libafl:
    workers: [local]
    resources:
      local:
        cpu: 16
        memory: 64G
    llm:
      max_budget: 150
      max_rpm: 1500
      max_tpm: 1500000

  atlantis-c-bullseye:
    workers: [local]
    resources:
      local:
        cpu: 16
        memory: 64G
    llm:
      max_budget: 150
      max_rpm: 1500
      max_tpm: 1500000
```

## Security

- **Never commit `.env` files** - they contain secrets
- **Use `.gitignore`** - template includes `.env` by default
- **Rotate credentials** - regularly update API keys and passwords
- **Limit permissions** - use least-privilege access for worker machines

## See Also

- [oss-crs Package Documentation](../oss-crs/CLAUDE.md)
- [CRS Interface Specification](../docs/ossfuzz-crs-interface.md)
- [Resource Configuration Guide](../oss-crs/example_configs/ensemble-c/RESOURCE_CONFIG_README.md)
- [CRSBench Documentation](../docs/benchmark-spec.md)
