# CRSBench - Cyber Reasoning System Benchmark Suite

A comprehensive benchmark suite for evaluating AI-powered Cyber Reasoning Systems (CRS) with standardized vulnerability discovery, program repair, and evaluation capabilities.

## Overview

CRSBench provides a standardized framework for evaluating Cyber Reasoning Systems across three core cybersecurity capabilities:

- **Vulnerability Discovery**: Automated identification of security vulnerabilities in source code
- **Program Repair**: Automated generation of patches to fix identified vulnerabilities
- **Evaluation**: Comprehensive assessment with ground truth validation

Unlike traditional fuzzing benchmarks (like FuzzBench) that only report coverage/crashes, CRSBench stores complete ground truth data to track whether vulnerabilities (POVs) are actually found or missed, enabling precise CRS evaluation.

## Logging

CRSBench uses a centralized logging system based on [loguru](https://loguru.readthedocs.io/) for consistent, colored output across all modules.

### Quick Logging Example

```python
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

logger.info("Application started")
logger.warning("Configuration missing")
logger.error("Operation failed")
```

### Features

- **Colored Output**: Automatic color-coded logs for different levels (DEBUG=blue, INFO=white, WARNING=yellow, ERROR=red)
- **Module Hierarchy**: Clear module paths like `[distributed/worker]`, `[evaluation/runner]`
- **TTY Detection**: Colors automatically disabled for file redirection
- **Environment Control**: Set log level via `LOG_LEVEL` environment variable

### Log Level Control

```bash
export LOG_LEVEL=DEBUG    # Show all messages
export LOG_LEVEL=INFO     # Default level
export LOG_LEVEL=WARNING  # Only warnings and errors
```

### Output Format

```
2025-11-21 11:24:23 | INFO     | [distributed/worker]            | Worker started
2025-11-21 11:24:24 | ERROR    | [evaluation/runner]             | Trial failed
2025-11-21 11:24:25 | SUCCESS  | [migration/repo_manager]        | Sync complete
```

### Logging Documentation

- **Usage Guide**: [docs/logger-usage-guide.md](docs/logger-usage-guide.md) - Comprehensive usage examples
- **Architecture**: [design-docs/logging/logging-architecture.md](design-docs/logging/logging-architecture.md) - Design and implementation details
- **Migration Summary**: [docs/logging-migration-summary.md](docs/logging-migration-summary.md) - Migration from standard logging

## Key Features

### **Unified Standard**
- **RFC Specification**: Comprehensive specification in `docs/benchmark-spec.md`
- **YAML-based Configuration**: Unified `meta.yaml` format for all benchmarks
- **Cross-Platform**: Compatible with Google OSS-Fuzz infrastructure

### **AI-Powered Infrastructure**
- **LLM Integration**: Built-in LiteLLM support for modern AI-powered CRS
- **Multi-Agent Workflows**: LangGraph-based agent orchestration
- **Intelligent Migration**: AI agents for format conversion and standardization

### **Comprehensive Evaluation**
- **Ground Truth Validation**: Complete POV (Proof of Vulnerability) tracking
- **Difficulty Control**: Progressive hint system for guided discovery
- **Multi-Modal Assessment**: Delta mode (focused) and full mode (comprehensive) evaluation

## Project Structure

```
CRSBench/
├── docs/                        # User-facing documentation
├── design-docs/                 # Internal architecture docs
├── benchmarks/                  # Benchmark projects in RFC format
├── crsbench/                    # Main Python package
│   ├── run_experiment.py        # CLI entry point
│   ├── builder/                 # OSS-Fuzz variant building
│   ├── evaluation/              # CRS execution & verification
│   │   └── verification/        # POV & patch verification engines
│   ├── benchmark_ci/            # Benchmark CI validation
│   ├── distributed/             # Distributed execution (Redis/RQ)
│   ├── benchmark/               # Benchmark packaging, canary, seed
│   ├── validation/              # Format validation & schemas
│   ├── migration/               # Format migration tools
│   ├── hint_generation/         # Progressive hint generation
│   ├── reporting/               # Report generation & dashboard
│   ├── statistics/              # Benchmark statistics
│   └── utils/                   # Shared utilities
├── crses/                       # CRS configurations
├── oss-fuzz/                    # OSS-Fuzz (submodule)
└── pyproject.toml               # Project configuration
```

## CRS Configuration Structure

CRSBench uses two directories for managing CRS (Cyber Reasoning System) configurations:

### `oss-crs-registry/` - The CRS Registry (Submodule)
- **Purpose**: The **ONLY** registry for CRS implementations (used for both testing and production)
- **Use Case**: Development, testing, and production evaluation
- **Source**: Git submodule from the open-source CRS registry
- **Structure**: Contains `crs/` directory with CRS configurations
- **Registry structure**: `oss-crs-registry/crs/<crs-name>/`

### `crses/` - CRS Configuration Directory
- **Purpose**: Directory containing CRS configurations for CRSBench evaluation
- **Use Case**: Stores final CRS configs that CRSBench will evaluate
- **Source**: Committed to CRSBench repository
- **Structure**: Follows the same format as `oss-crs/example_configs/` (NOT a registry)
- **Configuration structure**: `crses/<crs-name>/`

**Important**: `crses/` is NOT a registry - it's simply a directory of CRS configurations following the same format as `oss-crs/example_configs/`. The `oss-crs-registry/` is the only actual registry.

Both directories contain CRS configuration subdirectories with:
- `config-crs.yaml` - CRS runtime configuration
- `config-litellm.yaml` - LiteLLM configuration (optional)
- `config-resource.yaml` - Resource limits (optional)
- `config-worker.yaml` - Worker configuration (optional)

Example CRS configuration structure (same format in both `crses/` and `oss-crs-registry/crs/`):
```
<crs-name>/                   # e.g., atlantis-c-deepgen/
├── config-crs.yaml          # CRS runtime configuration
├── config-litellm.yaml      # LiteLLM settings (optional)
├── config-resource.yaml     # Resource limits (optional)
└── config-worker.yaml       # Worker settings (optional)
```

See `oss-crs/example_configs/` for reference examples of CRS configuration format.

## Quick Start

### Installation

```bash
# Clone the repository
git clone <repository-url>
cd CRSBench

# Install with uv (recommended)
uv venv
uv pip install -e .

# Or with pip
pip install -e .
```

### Environment Configuration

CRSBench supports environment variable configuration through `.env` files for easy local development and deployment:

```bash
# Copy the example configuration file
cp .env.example .env

# Edit .env with your settings
# The .env file is automatically loaded when CRSBench runs
```

**Common environment variables:**

- `REDIS_HOST` - Redis server for distributed execution (default: localhost)
- `LITELLM_BASE_URL` - LiteLLM API endpoint for AI-powered CRS
- `LITELLM_MASTER_KEY` - Authentication key for LiteLLM
- `OSS_FUZZ_PATH` - Path to OSS-Fuzz installation
- `LOG_LEVEL` - Logging verbosity (DEBUG, INFO, WARNING, ERROR)

See `.env.example` for a complete list of supported configuration options.

**Note:** The `.env` file is optional. You can also set environment variables directly in your shell or CI/CD pipeline.

### Basic Usage

```python
from crsbench.validation import validate_benchmark

# Validate a benchmark configuration
result = validate_benchmark("benchmarks/example-project")

if result.is_valid:
    print(f"Valid benchmark with {result.metadata['total_povs']} POVs")
else:
    print(f"Validation failed: {result.error_count} errors")
    for error in result.errors:
        print(f"  - {error}")
```

## Benchmark Format

CRSBench uses a standardized RFC format with `meta.yaml` configuration:

```yaml
# Comprehensive patch exclusion list
patch_exclude_list:
  - "build.sh"
  - "test/**"
  - "**/*test*.c"

# Evaluation modes
full_mode:
  base_commit: "abc123def456"

delta_mode:
  base_commit: "abc123def456"
  ref_commit: "def456abc123"

# Harness specifications with embedded POVs
harness_files:
  - name: "fuzz_parser"
    path: "/src/project/test/fuzz_parser.c"
    vulns:
      - vuln_keyword: "buffer_overflow_main"
        povs:
          - id: "pov_0"
            sanitizer: "address"
            error_token: "AddressSanitizer: heap-buffer-overflow"
```

## Core Components

### **Validation Module** (`crsbench.validation`)
Robust format validation with minimal side effects, designed for agent tool calls:
- Pure functions with no side effects
- Comprehensive error reporting with structured codes
- JSON-serializable results for agent communication
- Thread-safe concurrent validation

### **Migration Module** (`crsbench.migration`)
AI-powered migration system using LangGraph agents:
- Multi-agent workflow for format conversion
- Intelligent data mapping and transformation
- Preserves all vulnerability data and configurations
- Batch processing capabilities

### **Hint Generation Module** (`crsbench.hint_generation`)
Progressive hint generation from POV data:
- 4-level hint system (general to specific)
- AI analysis of crash logs and code patches
- Difficulty control for CRS training
- Educational vulnerability discovery guidance

### **Utilities Module** (`crsbench.utils`)
Shared utilities for all components:
- YAML handling and configuration management
- File operations and path utilities
- Logging and error handling
- Common data structures

### **Builder Module** (`crsbench.builder`)
Unified OSS-Fuzz variant building:
- Parallel variant builds with configurable workers
- Support for all variant types (base, ref, allpatched, cpv, coverage)
- Build caching and staleness detection
- Patch application for CPV variants

### **Evaluation Module** (`crsbench.evaluation`)
Runtime evaluation and verification:
- **POV Verification**: Verify POVs against benchmark variants to identify CPVs
- **Patch Verification**: Validate CRS-generated patches fix vulnerabilities without regressions
- **Coverage**: Code coverage collection and analysis (LLVM for C/C++, JaCoCo for Java)
- **CRS Execution**: Run CRS implementations against benchmarks
- Parallel verification with configurable workers

### **Distributed Module** (`crsbench.distributed`)
Multi-machine experiment execution via Redis/RQ:
- Three-process model: orchestrator enqueues, workers execute trials, evaluator verifies
- Automatic job distribution with worker-level config overrides
- Fault-tolerant with Redis-backed job queue

### **Benchmark CI Module** (`crsbench.benchmark_ci`)
Automated benchmark validation pipeline:
- Format, build, POV, and patch verification stages
- Flat DAG execution with per-variant parallelism
- Distributed builds via Redis for large benchmark sets

## CLI Interface

CRSBench provides a unified CLI (`crsbench`) for running experiments, verifying results, and generating reports.

### Running Experiments

```bash
# Run CRS experiments (local or distributed)
crsbench run --experiment-config config.yaml --benchmarks bench1 --crses crs1

# Process trial jobs from Redis queue (distributed worker)
crsbench worker --experiment-config config.yaml -j 4

# Verify POVs from completed trials (distributed evaluator)
crsbench evaluator --experiment-config config.yaml --experiment-name my-exp
```

### Verification (Standalone)

```bash
# Verify POVs against benchmark variants
crsbench verify benchmarks/project --pov-dir ./povs/

# Verify CRS-generated patches
crsbench patch-verify benchmarks/project --patch-dir ./patches --pov-dir ./povs

# Collect code coverage
crsbench coverage benchmarks/project --corpus-dir ./corpus/

# Re-verify existing trial outputs
crsbench re-eval --experiment-config config.yaml
```

### Results & Reporting

```bash
# Generate reports (JSON/HTML)
crsbench report --experiment my-exp

# Launch web dashboard
crsbench dashboard --base-dir ./experiments

# Export benchmark statistics
crsbench stats --output stats.csv
```

## Distributed Execution

CRSBench supports multi-machine experiment execution using a three-process model backed by Redis/RQ:

1. **Orchestrator** (`crsbench run`) — enqueues trial jobs onto the Redis queue
2. **Worker** (`crsbench worker`) — pulls and executes trial jobs
3. **Evaluator** (`crsbench evaluator`) — builds variants and verifies POVs from completed trials

See [Distributed Execution Guide](docs/distributed-execution.md) for setup and usage details.

## AI Infrastructure

CRSBench includes comprehensive LLM integration:

### Dependencies
- **LangChain**: LLM framework for building AI applications
- **LangGraph**: Multi-agent workflow orchestration
- **LiteLLM**: Universal API interface (OpenAI, Anthropic, etc.)
- **Pydantic**: Data validation and schema management

### Agent Workflows
- **Migration Agents**: Analyze, convert, and validate benchmark formats
- **Hint Generation Agents**: Extract insights from POV data
- **Validation Agents**: Ensure benchmark quality and completeness

## Glossary

- **CRS**: Cyber Reasoning System - AI system for cybersecurity tasks
- **POV**: Proof of Vulnerability - Input data that triggers a crash/vulnerability
- **Harness**: Test program that exercises vulnerable code with fuzzing inputs
- **Delta Mode**: Focused evaluation on specific code changes between commits
- **Full Mode**: Comprehensive evaluation of entire vulnerable codebase
- **RFC**: The standardized specification document for CRS benchmarks

## Roadmap

See [GitHub Issues](https://github.com/sslab-gatech/CRSBench/issues) for planned features and progress.

## Development

### Code Quality Checks

Run quality checks manually with:

```bash
just check        # Run all checks (typecheck + lint + format)
just typecheck    # Type checking only
just lint         # Linting only
just format       # Auto-format code
just lint-fix     # Auto-fix linting issues
```

### Pre-commit Hooks

Set up pre-commit hooks to automatically run checks before each commit:

```bash
# Install pre-commit
uv pip install pre-commit

# Install the hooks
pre-commit install

# Run manually on all files
pre-commit run --all-files
```

## For Developers

### Benchmark Management (`crsbench benchmark`)

```bash
# Validate benchmark structure
crsbench benchmark validate benchmarks/project

# Create pkgs/ tarball
crsbench benchmark bundle benchmarks/project

# Bundle all benchmarks in parallel
crsbench benchmark bundle-all benchmarks/ --workers 8

# Generate ref.diff for delta-mode
crsbench benchmark prepare-delta benchmarks/project

# Add contamination detection canary
crsbench benchmark inject-canary benchmarks/ --filter "atlanta-*"

# List registered canary UUIDs
crsbench benchmark list-canaries

# Import corpus from experiment output as seeds
crsbench benchmark seed-import --experiment-dir ./experiments/my-exp
```

### Benchmark CI (`crsbench ci`)

```bash
# Validate format (no Docker)
crsbench ci format --all

# Build variant images
crsbench ci build --all

# Verify ground-truth POVs
crsbench ci pov --all

# Verify ground-truth patches
crsbench ci patch --all

# Run all checks
crsbench ci all --all

# Distributed builds
crsbench ci build --all --distributed --redis-host localhost
```

## Contributing

1. **Environment Setup**: Use `uv` as the package manager
2. **Code Standards**: Follow existing patterns and conventions
3. **Pre-commit**: Install pre-commit hooks to ensure code quality
4. **Testing**: Validate all changes with the validation module
5. **Documentation**: Update relevant README files

## License

[To be determined]

## References

- [RFC Specification](docs/benchmark-spec.md)
- [Validation Module](crsbench/validation/README.md)
- [Migration Module](crsbench/migration/README.md)
- [Hint Generation Module](crsbench/hint_generation/README.md)
- [Distributed Execution Guide](docs/distributed-execution.md)
- [Distributed Evaluation Design](design-docs/distributed/distributed-evaluation.md)

---

**Related Projects**:
- [FuzzBench](https://github.com/google/fuzzbench) - Fuzzer evaluation platform
- [OSS-Fuzz](https://github.com/google/oss-fuzz) - Continuous fuzzing for open source
- [AIxCC Competition](https://www.ai.darpa.mil/programs/artificial-intelligence-cyber-challenge) - DARPA AI Cyber Challenge