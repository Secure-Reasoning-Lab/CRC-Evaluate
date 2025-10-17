# CRSBench - Cyber Reasoning System Benchmark Suite

A comprehensive benchmark suite for evaluating AI-powered Cyber Reasoning Systems (CRS) with standardized vulnerability discovery, program repair, and evaluation capabilities.

## Overview

CRSBench provides a standardized framework for evaluating Cyber Reasoning Systems across three core cybersecurity capabilities:

- **Vulnerability Discovery**: Automated identification of security vulnerabilities in source code
- **Program Repair**: Automated generation of patches to fix identified vulnerabilities
- **Evaluation**: Comprehensive assessment with ground truth validation

Unlike traditional fuzzing benchmarks (like FuzzBench) that only report coverage/crashes, CRSBench stores complete ground truth data to track whether vulnerabilities (POVs) are actually found or missed, enabling precise CRS evaluation.

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
├── docs/
│   └── benchmark-spec.md         # RFC specification for CRS benchmarks
├── benchmarks/                   # Standard benchmark projects in RFC format
├── crsbench/                    # Main Python package
│   ├── utils/                   # Shared utilities
│   ├── validation/              # Benchmark format validation
│   ├── migration/               # Format migration tools (Team-Atlanta to RFC)
│   └── hint_generation/         # Progressive hint generation
└── pyproject.toml               # Python project configuration
```

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
- `LITELLM_URL` - LiteLLM API endpoint for AI-powered CRS
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

## Development Status

### **Completed**
- [x] Unified YAML specification (RFC)
- [x] Python package structure with uv
- [x] Comprehensive validation module
- [x] LLM infrastructure setup
- [x] Documentation framework

### **In Progress**
- [ ] Migration agent implementation
- [ ] Hint generation agent implementation
- [ ] Benchmark format migrations
- [ ] Integration testing

### **Planned**
- [ ] CLI interface
- [ ] Web dashboard
- [ ] Performance benchmarking
- [ ] Community benchmark submissions

## Contributing

1. **Environment Setup**: Use `uv` as the package manager
2. **Code Standards**: Follow existing patterns and conventions
3. **Testing**: Validate all changes with the validation module
4. **Documentation**: Update relevant README files

## License

[To be determined]

## References

- [RFC Specification](docs/benchmark-spec.md)
- [Validation Module](crsbench/validation/README.md)
- [Migration Module](crsbench/migration/README.md)
- [Hint Generation Module](crsbench/hint_generation/README.md)

---

**Related Projects**:
- [FuzzBench](https://github.com/google/fuzzbench) - Fuzzer evaluation platform
- [OSS-Fuzz](https://github.com/google/oss-fuzz) - Continuous fuzzing for open source
- [AIxCC Competition](https://www.ai.darpa.mil/programs/artificial-intelligence-cyber-challenge) - DARPA AI Cyber Challenge