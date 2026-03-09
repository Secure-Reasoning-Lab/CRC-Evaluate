# Evaluation Module Design

This document provides detailed implementation documentation for the `crsbench.evaluation` module.

## Purpose

The evaluation module orchestrates the execution of CRS (Cyber Reasoning System) implementations against benchmarks, collects results, and generates comprehensive evaluation reports. It provides a standardized interface for running evaluations and tracking POV (Proof of Vulnerability) detection.

## Architecture Overview

### Module Structure

```
crsbench/evaluation/
├── __init__.py              # Public API exports
├── runner.py                # BenchmarkRunner orchestration
├── adapter/                 # CRS adapter interface
│   ├── __init__.py          # Public API (create_adapter, OssCrsAdapter)
│   └── oss_crs.py           # OssCrsAdapter and create_adapter()
├── results.py               # Result data structures
└── errors.py                # Evaluation-specific errors
```

### Design Philosophy

1. **Standardized Interface**: Consistent API for all CRS implementations
2. **Comprehensive Tracking**: Full POV detection status (found/missed/error)
3. **Flexible Execution**: Support for delta mode and full mode evaluation
4. **Resource Monitoring**: Track execution time and resource usage
5. **Extensible**: Easy to add new CRS implementations

### Snapshot Support

Evaluation supports periodic trial snapshots for progress tracking and partial-result inspection.
See [docs/design/evaluation/snapshots.md](./snapshots.md) for snapshot format, capture lifecycle, and usage.

## Distributed Evaluator and CI Result Semantics

### Evaluator `--ci` behavior

- `crsbench evaluator --ci` is a compatibility alias.
- It now uses the same unified configless evaluator path as standard configless mode (`run_evaluator_configless`) and the same multi-queue supervisor path.
- The only CI-specific behavior is queue selection: it targets legacy CI build/verify queues via the alias mode.

### CI result status semantics

- Dependency and infrastructure failures are surfaced as `ERROR` (not `FAIL`) in aggregate CI results.
- This includes explicit dependency-failed job status and failed jobs with `error_code` prefixes such as `infra_`, `dependency_`, or `dep_`.
- Aggregate benchmark status gives `ERROR` precedence over `FAIL`.

### Split vs combined result fallback

- Split POV/Patch checks are authoritative only when the full split set is present.
- If split data is partial and a legacy combined check exists, the combined check remains authoritative for aggregate status/time.
- If no combined check exists, available split checks are used.

## Core Components

### BenchmarkRunner (runner.py)

Orchestrates the evaluation process across all harnesses in a benchmark.

**Key Responsibilities**:
- Load and validate benchmark configuration
- Determine evaluation mode (auto/delta/full)
- Execute CRS on each harness
- Collect and aggregate results
- Generate evaluation reports

**Key Methods**:
```python
class BenchmarkRunner:
    def __init__(self, adapter: OssCrsAdapter):
        """Initialize with CRS adapter."""

    def run_benchmark(
        self,
        benchmark_path: Path,
        mode: str = "auto",
        crs_config: Optional[Dict[str, Any]] = None
    ) -> EvaluationResult:
        """Run CRS evaluation on benchmark."""
```

**Evaluation Flow**:
```
1. Load benchmark configuration (meta.yaml)
2. Validate configuration
3. Determine evaluation mode
4. Configure CRS
5. For each harness:
   - Execute CRS
   - Process POV results
   - Record execution details
6. Aggregate results
7. Generate evaluation report
```

### OssCrsAdapter (adapter/oss_crs.py)

Unified adapter for CRS execution supporting both bug-finding and bug-fixing modes via a `mode` parameter.

**Interface Definition**:
```python
class OssCrsAdapter:
    def __init__(self, mode: str, crs_config_name: str, ...):
        """Initialize adapter with mode ('bug-finding' or 'bug-fixing')."""

    def configure(self, config: Dict[str, Any]) -> None:
        """Configure CRS with parameters."""

    def prepare(self, benchmark_path: Path, harness: HarnessFile, ...) -> None:
        """Run crs-compose prepare phase."""

    def build_target(self, ...) -> None:
        """Run crs-compose build-target phase."""

    def run(self, ...) -> CRSExecutionResult:
        """Run crs-compose run phase and collect results."""
```

**Factory Function**:
```python
def create_adapter(config: ExperimentConfig, crs_config_name: str, ...) -> OssCrsAdapter:
    """Create an OssCrsAdapter from experiment configuration."""
```

**Design Decisions**:

- **Single class with mode parameter**: OssCrsAdapter handles both bug-finding and bug-fixing via `mode`
- **Three-phase lifecycle**: crs-compose prepare/build-target/run phases
- **Harness-level execution**: Each harness is evaluated independently
- **Support for both modes**: `mode` parameter selects bug-finding or bug-fixing behavior

### Testing with Mock Adapters

For testing, use `unittest.mock.MagicMock` configured to return `CRSExecutionResult`:

```python
from unittest.mock import MagicMock
from crsbench.evaluation.results import CRSExecutionResult

mock_adapter = MagicMock()
mock_adapter.mode = "bug-finding"
mock_adapter.run.return_value = CRSExecutionResult(...)
```

**Use Cases**:
- Testing evaluation pipeline
- Demonstrating evaluation workflow
- Benchmarking runner performance
- Validating report generation

### Result Data Structures (results.py)

#### POVStatus Enum
```python
class POVStatus(str, Enum):
    FOUND = "found"      # POV successfully detected
    MISSED = "missed"    # POV not detected
    ERROR = "error"      # Execution error prevented detection
```

#### POVResult
```python
@dataclass
class POVResult:
    name: str                    # POV identifier
    harness_name: str            # Associated harness
    sanitizer: str               # Sanitizer type
    error_token: Optional[str]   # Expected error pattern
    status: POVStatus            # Detection status
    execution_time: float = 0.0  # Time spent
    error_message: Optional[str] = None
    crs_output: Optional[str] = None
```

#### HarnessResult
```python
@dataclass
class HarnessResult:
    harness_name: str
    success: bool                # Execution success
    execution_time: float
    pov_results: List[POVResult]
    error_message: Optional[str] = None
```

#### EvaluationReport
```python
@dataclass
class EvaluationReport:
    # Summary metrics
    total_harnesses: int
    total_povs: int
    povs_found: int
    povs_missed: int
    povs_error: int
    success_rate: float

    # Detailed results
    harness_results: List[HarnessResult]

    # Configuration
    benchmark_path: str
    mode: str
    base_commit: str
    ref_commit: Optional[str]
    crs_config: Dict[str, Any]

    # Timing
    total_execution_time: float
    timestamp: str

    # Export methods
    def to_dict(self) -> Dict[str, Any]
    def save_json(self, path: Path)
    def save_yaml(self, path: Path)
```

## Evaluation Modes

### Auto Mode
Automatically selects the best available evaluation mode:
1. Prefers delta mode if available
2. Falls back to full mode
3. Raises error if neither is configured

**When to Use**: Default choice for most evaluations

### Delta Mode
Evaluates CRS on specific code changes between two commits.

**Configuration**:
```yaml
delta_mode:
  base_commit: "abc123def456789"
  ref_commit: "def456789abc123"
```

**Behavior**:
- CRS receives both base and reference commits
- Focus on changes between commits
- Suitable for patch validation

**When to Use**: Testing CRS ability to find vulnerabilities in diffs

### Full Mode
Evaluates CRS on complete codebase at a specific commit.

**Configuration**:
```yaml
full_mode:
  base_commit: "abc123def456789"
```

**Behavior**:
- CRS receives entire codebase
- No reference commit
- Comprehensive vulnerability discovery

**When to Use**: Baseline evaluation, full capability assessment

## Integration with OSS-Fuzz Interface

The evaluation module is designed to integrate with the OSS-Fuzz CRS interface for actual CRS implementations.

**See [OSS-CRS Integration](./oss-crs-integration.md) for complete implementation details** covering:
- `OssCrsAdapter` with `mode` parameter for bug-finding and bug-fixing
- Docker integration and container management via crs-compose
- POV detection logic and crash analysis
- Configuration management
- Build caching strategy

### Adapter Overview

`OssCrsAdapter` wraps the crs-compose interface for both bug-finding and bug-fixing:

```python
# Bug Finding
adapter = OssCrsAdapter(mode="bug-finding", crs_config_name="ensemble-c", ...)
# Runs: crs-compose prepare/build-target/run for bug finding

# Bug Fixing (Patch Generation)
adapter = OssCrsAdapter(mode="bug-fixing", crs_config_name="multi-retrieval", ...)
# Runs: crs-compose prepare/build-target/run for patch generation
```

### Usage Example

```python
from crsbench.evaluation.adapter import create_adapter, OssCrsAdapter

# Create adapter via factory function
adapter = create_adapter(
    config=experiment_config,
    crs_config_name="ensemble-c",
    oss_fuzz_path=Path("/path/to/oss-fuzz"),
    registry_dir=Path("/path/to/registry"),
    benchmarks_root=Path("/path/to/benchmarks"),
)

# Run evaluation
runner = BenchmarkRunner(adapter=adapter)
result = runner.run_benchmark(
    benchmark_path=Path("benchmarks/json-c"),
    mode="auto"
)
```

See [OSS-CRS Integration](./oss-crs-integration.md) and [OSS-CRS Interface](../../reference/oss-crs-interface.md) for complete details.

## Error Handling

### Error Types

**EvaluationError**: Base exception for evaluation failures
- Raised when evaluation cannot proceed
- Includes context about failure point

**Execution Errors**: Individual harness failures
- Captured in HarnessResult.error_message
- Does not stop evaluation of other harnesses
- Marked as POVStatus.ERROR

### Error Handling Strategy

1. **Validation Failures**: Raise EvaluationError before starting evaluation
2. **CRS Configuration Errors**: Raise EvaluationError during setup
3. **Harness Execution Errors**: Capture in results, continue evaluation
4. **POV Processing Errors**: Mark affected POVs as ERROR status

### Graceful Degradation

When a harness fails:
1. Record error in HarnessResult
2. Mark all POVs for that harness as ERROR
3. Continue evaluating remaining harnesses
4. Include partial results in final report

## Testing Strategy

### Test Location

**File**: `tests/test_evaluation.py`

**IMPORTANT**: When updating the evaluation module, you **MUST** update and run `tests/test_evaluation.py`.

### Test Categories

#### 1. BenchmarkRunner Tests
```python
def test_runner_auto_mode():
    """Test automatic mode selection."""

def test_runner_delta_mode():
    """Test delta mode evaluation."""

def test_runner_full_mode():
    """Test full mode evaluation."""

def test_runner_invalid_mode():
    """Test error handling for invalid mode."""
```

#### 2. OssCrsAdapter Tests
```python
def test_adapter_configuration():
    """Test CRS adapter configuration."""

def test_adapter_execution():
    """Test harness execution via adapter."""

def test_adapter_mode_selection():
    """Test bug-finding vs bug-fixing mode."""
```

#### 3. Result Collection Tests
```python
def test_pov_result_creation():
    """Test POV result data structure."""

def test_harness_result_aggregation():
    """Test harness result collection."""

def test_evaluation_report_generation():
    """Test report generation and metrics."""
```

#### 4. Integration Tests
```python
def test_end_to_end_evaluation():
    """Test complete evaluation flow with test_benchmark."""

def test_evaluation_with_errors():
    """Test evaluation with harness failures."""

def test_report_export():
    """Test JSON/YAML export functionality."""
```

### Running Tests

```bash
# Run all evaluation tests
pytest tests/test_evaluation.py -v

# Run with coverage
pytest tests/test_evaluation.py --cov=crsbench.evaluation --cov-report=html

# Run specific test
pytest tests/test_evaluation.py::test_runner_auto_mode -v
```

## Performance Considerations

### Execution Time
- Serial execution: One harness at a time
- Execution time scales linearly with number of harnesses
- CRS execution dominates total time

### Future Optimizations
- Parallel harness execution (when CRS supports it)
- Result streaming for long-running evaluations
- Incremental reporting

### Resource Tracking
- Per-harness execution time
- Total evaluation time
- Future: LLM token usage tracking
- Future: Memory and CPU monitoring

## Integration with Other Modules

### Validation Module
```python
from crsbench.validation import validate_benchmark

def run_benchmark(self, benchmark_path: Path, ...):
    # Validate before evaluation
    result = validate_benchmark(benchmark_path)
    if not result.is_valid:
        raise EvaluationError(f"Invalid benchmark: {result.summary()}")
```

### Orchestrator (run_experiment.py)
```python
from crsbench.evaluation import BenchmarkRunner
from crsbench.evaluation.adapter import create_adapter

# Orchestrator creates runners for each CRS
adapter = create_adapter(config, crs_name, oss_fuzz_path, registry_dir, benchmarks_root)
runner = BenchmarkRunner(adapter=adapter)
result = runner.run_benchmark(benchmark_path, mode="auto", crs_config=config)
```

### Distributed Module
```python
# Jobs can be distributed across workers
def run_trial(trial_config):
    adapter = create_adapter(trial_config.config, trial_config.crs, ...)
    runner = BenchmarkRunner(adapter=adapter)
    return runner.run_benchmark(trial_config.benchmark, ...)
```

## Design Decisions

### Why OssCrsAdapter with Mode Parameter?

**Problem**: Need to support both bug-finding and bug-fixing CRS workflows with a consistent interface.

**Solution**: Single `OssCrsAdapter` class with a `mode` parameter ("bug-finding" or "bug-fixing") and a `create_adapter()` factory function.

**Benefits**:
- Standardized evaluation process for both modes
- Single adapter class reduces code duplication
- Factory function encapsulates creation logic
- Mock-friendly for testing (use `MagicMock` with `CRSExecutionResult`)

### Why Separate POV Processing?

**Problem**: Different CRS implementations produce different output formats.

**Solution**: Each CRS executor implements `process_pov_results()` to map its output to standard POVResult.

**Benefits**:
- Flexible output parsing
- Standardized result format
- CRS-specific detection logic
- Easier to debug detection failures

### Why Three POV Statuses?

**Problem**: Need to distinguish between "CRS didn't find it" and "CRS crashed before it could look".

**Solution**: Three statuses: FOUND, MISSED, ERROR.

**Benefits**:
- Accurate success rate calculation (exclude ERRORs)
- Identify infrastructure vs. capability issues
- Better debugging information
- Fair CRS comparison

### Why Per-Harness Execution?

**Problem**: Should we run CRS once for entire benchmark or once per harness?

**Solution**: Execute CRS separately for each harness.

**Benefits**:
- Isolation: Harness failures don't affect others
- Parallelization: Can run harnesses concurrently
- Granular results: Per-harness timing and errors
- Match OSS-Fuzz interface (per-harness execution)

**Trade-off**: More CRS invocations, but better isolation and parallelization potential.

## Future Enhancements

### Planned Features

1. **Parallel Execution**: Run multiple harnesses concurrently
2. **Resource Monitoring**: Track LLM tokens, memory, CPU usage
3. **Streaming Results**: Real-time result updates for long evaluations
4. **Result Comparison**: Compare results across CRS implementations
5. **Regression Detection**: Track performance changes over time
6. **Patch Evaluation**: Evaluate patch generation capabilities

### Extension Points

- Custom result collectors
- Alternative report formats (HTML, CSV)
- Integration with experiment tracking systems
- Custom metrics and scoring

## Common Pitfalls

### 1. Not Validating Before Evaluation

**Wrong**:
```python
runner = BenchmarkRunner(adapter)
result = runner.run_benchmark(path)  # May fail with cryptic error
```

**Right**:
```python
validation_result = validate_benchmark(path)
if not validation_result.is_valid:
    raise EvaluationError(f"Invalid benchmark: {validation_result.summary()}")

runner = BenchmarkRunner(adapter)
result = runner.run_benchmark(path)
```

### 2. Ignoring Execution Errors

**Wrong**:
```python
if result.povs_found > 0:
    print("Success!")  # Might have had errors!
```

**Right**:
```python
if result.povs_error > 0:
    print(f"Warning: {result.povs_error} POVs had execution errors")
success_rate = result.povs_found / (result.total_povs - result.povs_error)
```

### 3. Assuming Mode Availability

**Wrong**:
```python
result = runner.run_benchmark(path, mode="delta")  # May not be available
```

**Right**:
```python
result = runner.run_benchmark(path, mode="auto")  # Auto-detect
# Or check first
validation = validate_benchmark(path)
if validation.metadata["has_delta_mode"]:
    result = runner.run_benchmark(path, mode="delta")
```

## References

- [RFC Specification](../../RFC.md): Benchmark format specification
- [Architecture](../architecture.md): Overall CRSBench architecture
- [OSS-CRS Interface](../../reference/oss-crs-interface.md): CRS interface specification
- [Validation Module](../validation/validation.md): Validation module design
