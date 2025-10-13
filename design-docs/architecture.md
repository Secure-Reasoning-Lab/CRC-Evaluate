# CRSBench Architecture

This document describes the overall architecture and implementation details of CRSBench, a benchmark suite for evaluating Cyber Reasoning Systems (CRS).

## Overview

CRSBench is a Python-based framework that provides:
- Standardized benchmark format for CRS evaluation
- Validation and migration tools
- Experiment running infrastructure
- Evaluation and reporting capabilities

## Repository Structure

```
CRSBench/
├── crsbench/                   # Main Python package
│   ├── run_experiment.py       # CLI entry point (crsbench command)
│   ├── validation/             # Benchmark format validation
│   ├── evaluation/             # CRS evaluation and scoring
│   ├── builder/                # Docker image building for benchmarks
│   ├── reproducer/             # POV reproduction and verification
│   ├── patch_tester/           # Patch validation and testing
│   ├── deduplication/          # POV deduplication logic
│   ├── migration/              # Format migration tools
│   ├── hint_generation/        # Hint generation for difficulty control
│   └── utils/                  # Shared utilities
├── benchmarks/                 # Benchmark projects (new format)
├── benchmarks-internal/        # Internal testing benchmarks (old format)
├── benchmarks-afc/             # Official AIxCC benchmarks
├── experiments/                # Experiment configurations and results
├── crses/                      # CRS implementations
├── docs/                       # RFC specifications and user docs
│   ├── benchmark-spec.md       # RFC for benchmark format
│   └── meta-example.yaml       # Example configuration
├── design-docs/                # Implementation documentation (this file)
├── test_benchmark/             # Test benchmark for development
└── pyproject.toml              # Package configuration

```

## Core Modules

### 1. validation/
**Purpose**: Validate benchmark configurations against the RFC specification.

**Key Components**:
- `schemas.py`: Pydantic models for type-safe validation
- `format_validator.py`: Pure validation functions with minimal side effects
- `errors.py`: Structured error types and codes
- `README.md`: Module documentation and usage examples

**Design Philosophy**:
- Pure functions with no side effects
- Safe for LLM agent tool calls
- Graceful error handling with detailed reporting

See [design-docs/validation.md](./validation.md) for detailed documentation.

### 2. evaluation/
**Purpose**: Run CRS evaluations and generate reports.

**Responsibilities**:
- Execute CRS implementations against benchmarks
- Track resource usage (LLM tokens, compute time)
- Collect POVs and patches
- Generate evaluation reports

**Key Features**:
- BenchmarkRunner for orchestrating evaluations
- CRSExecutor interface for CRS implementations
- StubCRSExecutor for testing
- Result aggregation and reporting

### 3. builder/
**Purpose**: Build Docker images for benchmark projects.

**Responsibilities**:
- OSS-Fuzz compatible image building
- Layered Docker architecture (base → project → evaluation)
- Patch application and incremental builds
- Harness compilation and artifact management

**Architecture**:
```
OSS-Fuzz Base Image
    ↓
Project Base Image (pre-compiled dependencies)
    ↓
Evaluation Image (with applied patches)
```

### 4. reproducer/
**Purpose**: Reproduce and verify POVs.

**Responsibilities**:
- Execute harnesses with POV inputs
- Capture sanitizer output
- Verify expected crashes
- Match error tokens for deduplication

### 5. patch_tester/
**Purpose**: Validate CRS-generated patches.

**Responsibilities**:
- Apply patches to source code
- Check patch exclusion list compliance
- Run invariant tests (test.sh)
- Verify POVs no longer crash
- Ensure program functionality maintained

### 6. deduplication/
**Purpose**: Deduplicate discovered POVs.

**Responsibilities**:
- Group POVs by root cause analysis
- Match sanitizer error signatures
- Handle multiple variants of same vulnerability
- Support deduplication across different sanitizers

**Key Insight**: Multiple POVs with same sanitizer error may be different bugs; multiple POVs with different stack traces may be same bug.

### 7. migration/
**Purpose**: Migrate benchmarks to new format.

**Responsibilities**:
- Convert old internal format to RFC format
- Migrate AIxCC benchmarks to RFC format
- Validate migrated benchmarks
- Preserve ground truth data

### 8. hint_generation/
**Purpose**: Generate hints for difficulty control.

**Responsibilities**:
- Generate progressive hints (levels 1-4)
- Create corpus variations (levels 0-4)
- Maintain hint quality and specificity
- Support multiple hint types (category, location, severity, technical)

### 9. utils/
**Purpose**: Shared utilities across modules.

**Common Utilities**:
- File I/O helpers
- Git operations
- Docker utilities
- Logging configuration
- Path manipulation

## Data Flow

### Benchmark Creation Flow
```
1. Project Setup
   └→ Create benchmark directory structure
   └→ Write Dockerfile and build scripts
   └→ Add harness files

2. Ground Truth Creation
   └→ Run fuzzing to discover POVs
   └→ Create vulnerability directories (vuln-keyword/)
   └→ Save POV variants (blob + log files)
   └→ Generate patches

3. Configuration
   └→ Write meta.yaml with harness and vuln config
   └→ Validate with validation module
   └→ Add to benchmark suite

4. Quality Check
   └→ Run builder to verify image builds
   └→ Run reproducer to verify POVs trigger
   └→ Run patch_tester to verify patches fix
```

### Evaluation Flow
```
1. Experiment Setup
   └→ Load experiment config (trials, time limits, difficulty)
   └→ Select benchmarks from suite
   └→ Initialize CRS implementations

2. Evaluation Execution
   └→ For each benchmark:
       ├→ Provide to CRS (code, harnesses, hints based on difficulty)
       ├→ Track resource usage (tokens, compute time)
       ├→ Collect discovered POVs
       └→ Collect generated patches

3. Verification
   └→ Reproduce collected POVs
   └→ Deduplicate POVs by root cause
   └→ Test patches against ground truth
   └→ Check invariant tests pass

4. Scoring and Reporting
   └→ Calculate scores (difficulty × assistance level)
   └→ Generate reports (HTML, JSON, YAML)
   └→ Compare against baselines
   └→ Archive results
```

## CLI Entry Point

### crsbench Command
**Location**: `crsbench/run_experiment.py`

**Installation**:
```bash
uv pip install -e .
```

**Command Structure**:
```bash
crsbench \
  --experiment-config <YAML config> \
  --benchmarks <list or suite name> \
  --experiment-name <identifier> \
  --crses <list of CRS implementations>
```

**Configuration**: Defined in `pyproject.toml`:
```toml
[project.scripts]
crsbench = "crsbench.run_experiment:main"
```

## Integration Points

### Module Dependencies
```
run_experiment.py
    ├→ validation (validate experiment config)
    ├→ evaluation (run evaluations)
    │   ├→ builder (build Docker images)
    │   ├→ reproducer (verify POVs)
    │   ├→ patch_tester (test patches)
    │   └→ deduplication (deduplicate results)
    ├→ hint_generation (generate hints based on difficulty)
    └→ utils (shared utilities)
```

### External Integrations
- **Docker**: For isolated benchmark execution
- **LiteLLM**: For unified LLM API access and usage tracking
- **OSS-Fuzz**: Compatible build system and fuzzing infrastructure
- **Git**: For version control and commit-based evaluation modes

## Technology Stack

### Core Technologies
- **Python 3.11+**: Primary language
- **Pydantic 2.x**: Data validation and type safety
- **PyYAML**: Configuration file parsing
- **Docker**: Containerized execution
- **Git**: Version control integration

### Key Dependencies
- `pydantic>=2.11.9`: Schema validation
- `pyyaml>=6.0.2`: YAML parsing
- `gitpython>=3.1.44`: Git operations
- `litellm>=1.77.5`: LLM API integration
- `rich>=14.1.0`: Terminal output formatting
- `typer>=0.19.2`: CLI argument parsing (for subcommands)
- `click>=8.3.0`: CLI utilities

### Development Tools
- `pytest>=8.0.0`: Testing framework
- `pytest-cov>=6.0.0`: Coverage reporting
- `uv`: Fast Python package manager

## Design Principles

### 1. Minimal Side Effects
- Pure functions where possible (especially validation)
- Read-only operations for inspection tools
- Explicit state management

### 2. Type Safety
- Pydantic models for all configuration
- Type hints throughout codebase
- Runtime validation

### 3. Modularity
- Clear module boundaries
- Well-defined interfaces
- Independent module testing

### 4. Agent-Friendly
- Safe for LLM agent tool calls
- Structured, serializable results
- Comprehensive error reporting

### 5. Docker-First
- All execution in containers
- Reproducible environments
- Isolated evaluations

### 6. No Backward Compatibility
- Throw errors for old formats
- Force migration to new standard
- Clean break from legacy

## Configuration Files

### meta.yaml (Benchmark Configuration)
Location: `benchmarks/[project]/.aixcc/meta.yaml`

Structure:
```yaml
patch_exclude_list: [...]        # Files patches cannot modify
delta_mode: {...}                # Optional delta mode config
full_mode: {...}                 # Optional full mode config
harness_files:                   # Required
  - name: "harness_name"
    path: "/absolute/path/to/harness.c"
    vulns:                       # Optional
      - vuln_keyword: "buffer_overflow"
        difficulty_level: 3      # Optional 1-5
        povs:
          - id: "pov_0"
            sanitizer: "address"
            error_token: "..."   # Optional
```

### experiment-config.yaml (Experiment Configuration)
```yaml
trials: 1                        # Number of trials
max_total_time: 86400           # Seconds per trial
difficulty_level: 1              # Assistance level (0-4)
experiment_filestore: /path/    # Experiment data storage
report_filestore: /path/        # Report output directory
```

### pkg.yaml (CRS Package Configuration)
Location: `crses/[crs-name]/pkg.yaml`

Defines CRS installation requirements and dependencies.

### config-crs.yaml (CRS Runtime Configuration)
Location: `crses/[crs-name]/config-crs.yaml`

Defines CRS-specific runtime parameters and settings.

## Error Handling Strategy

### Validation Errors
- **Expected failures**: Return ValidationResult with errors
- **Process errors**: Raise ValidationError exception
- **Graceful degradation**: Continue validation to report all issues

### Evaluation Errors
- **CRS failures**: Capture and report, don't crash evaluation
- **Infrastructure failures**: Retry with backoff
- **Timeout handling**: Graceful termination with partial results

### Build Errors
- **Docker build failures**: Clear error messages with logs
- **Patch application failures**: Report file/line details
- **Compilation errors**: Capture compiler output

## Testing Strategy

### Unit Tests
- Each module has comprehensive unit tests
- Pydantic validation tests
- Pure function testing

### Integration Tests
- End-to-end evaluation flows
- Docker build and execution
- CRS interface testing

### Test Fixtures
- `test_benchmark/`: Sample benchmark for testing
- `example_*.py`: Example scripts demonstrating usage
- Mock CRS implementations (StubCRSExecutor)

## Performance Considerations

### Validation
- Fast execution (<100ms typical)
- Lazy loading
- Minimal memory footprint

### Evaluation
- Parallel benchmark execution
- Docker layer caching
- Incremental builds

### Resource Tracking
- LLM token counting (input, output, cached)
- Compute time tracking
- Memory profiling
- Cost calculation

## Future Extensions

### Planned Features
- Binary-only challenges
- Custom sanitizer support
- Fuzzing introspector integration
- Archive format for reproducibility
- Automated hint generation
- Progressive difficulty adaptation

### Extension Points
- Plugin architecture for new CRS types
- Custom evaluation metrics
- Alternative build systems
- New POV deduplication strategies

## Contributing Guidelines

When contributing to CRSBench:

1. **Read RFC first**: `docs/benchmark-spec.md`
2. **Check design docs**: This file and module-specific docs
3. **Use absolute imports**: No relative imports in Python
4. **Type everything**: Use type hints and Pydantic models
5. **Test thoroughly**: Unit tests for all new code
6. **Document changes**: Update relevant design docs
7. **Validate benchmarks**: Use validation module
8. **Follow standards**: No backward compatibility needed

## Related Documentation

- [RFC Specification](../docs/benchmark-spec.md): Benchmark format specification
- [Validation Module](./validation.md): Detailed validation documentation
- [Example Scripts](../example_*.py): Usage examples
- [Module READMEs](../crsbench/*/README.md): Per-module documentation
