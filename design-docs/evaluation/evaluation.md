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
├── crs_executor.py          # CRS executor interface and stub
├── results.py               # Result data structures
└── errors.py                # Evaluation-specific errors
```

### Design Philosophy

1. **Standardized Interface**: Consistent API for all CRS implementations
2. **Comprehensive Tracking**: Full POV detection status (found/missed/error)
3. **Flexible Execution**: Support for delta mode and full mode evaluation
4. **Resource Monitoring**: Track execution time and resource usage
5. **Extensible**: Easy to add new CRS implementations

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
    def __init__(self, crs_executor: CRSExecutor):
        """Initialize with CRS implementation."""

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

### CRSExecutor Interface (crs_executor.py)

Abstract base class defining the standard interface for CRS implementations.

**Interface Definition**:
```python
class CRSExecutor(ABC):
    @abstractmethod
    def configure_crs(self, config: Dict[str, Any]) -> None:
        """Configure CRS with parameters."""

    @abstractmethod
    def run_crs(
        self,
        benchmark_path: Path,
        harness: HarnessFile,
        base_commit: str,
        ref_commit: Optional[str] = None
    ) -> CRSResult:
        """Execute CRS on specific harness."""

    @abstractmethod
    def process_pov_results(
        self,
        crs_result: CRSResult,
        harness: HarnessFile
    ) -> List[POVResult]:
        """Determine POV detection status from CRS output."""
```

**Design Decisions**:

- **Separate configuration from execution**: Allows reconfiguration without reinstantiation
- **Harness-level execution**: Each harness is evaluated independently
- **Post-processing POV results**: CRS executor determines which POVs were detected
- **Support for both delta and full modes**: `ref_commit` parameter enables delta mode

### StubCRSExecutor (crs_executor.py)

Testing implementation that simulates CRS behavior for development and testing.

**Features**:
- Configurable simulation delay (execution time)
- Configurable success rate (POV detection probability)
- Realistic output generation
- Random failure simulation

**Configuration Parameters**:
```python
{
    "simulation_delay": 0.1,  # Seconds per harness
    "success_rate": 0.8       # POV detection rate (0.0-1.0)
}
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

**See [CRS Executors Design](./crs-executors.md) for complete implementation details** covering:
- `CRSBugFindingExecutor` for vulnerability discovery
- `CRSPatchExecutor` for patch generation
- Docker integration and container management
- POV detection logic and crash analysis
- Configuration management
- Build caching strategy
- Command migration to future formats

### Executor Overview

Concrete CRS executors wrap the OSS-Fuzz/OSS-Patch command-line interfaces:

```python
# Bug Finding Executor
class CRSBugFindingExecutor(CRSExecutor):
    """Wraps OSS-Fuzz bug finding interface."""
    # Build: oss-crs build <config-dir> <project>
    # Run: oss-crs run <config-dir> <project> <harness> [--output <dir>] [--hints <dir>]

# Patch Generation Executor
class CRSPatchExecutor(CRSExecutor):
    """Wraps OSS-Patch interface."""
    # Build: oss-bugfix-crs build <config> <project> --oss-fuzz $OSS_FUZZ_HOME \
    #        --project-path <benchmark-dir> --source-path <repo-manager-source>
    # Run: oss-bugfix-crs run <config> <project> --harness <name> \
    #      [--pov <file> | --povs <dir>] [--hints <dir>] [--output <dir>] --litellm-*
```

### Usage Example

```python
from crsbench.evaluation import BenchmarkRunner, CRSBugFindingExecutor

# Create executor
executor = CRSBugFindingExecutor(
    crs_config_name="ensemble-c",
    oss_fuzz_path=Path("/path/to/oss-fuzz")
)

# Run evaluation
runner = BenchmarkRunner(executor)
result = runner.run_benchmark(
    benchmark_path=Path("benchmarks/json-c"),
    mode="auto"
)
```

See [CRS Executors Design](./crs-executors.md) and [OSS-Fuzz CRS Interface](../docs/ossfuzz-crs-interface.md) for complete details.

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

#### 2. CRSExecutor Interface Tests
```python
def test_stub_executor_configuration():
    """Test CRS configuration."""

def test_stub_executor_execution():
    """Test harness execution."""

def test_stub_executor_pov_processing():
    """Test POV result processing."""
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

# Orchestrator creates runners for each CRS
runner = BenchmarkRunner(crs_executor)
result = runner.run_benchmark(benchmark_path, mode="auto", crs_config=config)
```

### Distributed Module
```python
# Jobs can be distributed across workers
def run_trial(trial_config):
    runner = BenchmarkRunner(create_crs_executor(trial_config.crs))
    return runner.run_benchmark(trial_config.benchmark, ...)
```

## Design Decisions

### Why Abstract CRSExecutor?

**Problem**: Need to support multiple CRS implementations with different interfaces.

**Solution**: Define abstract interface that all CRS implementations must follow.

**Benefits**:
- Standardized evaluation process
- Easy to add new CRS implementations
- Testing with stub implementation
- Clear contract for CRS developers

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
runner = BenchmarkRunner(crs)
result = runner.run_benchmark(path)  # May fail with cryptic error
```

**Right**:
```python
validation_result = validate_benchmark(path)
if not validation_result.is_valid:
    raise EvaluationError(f"Invalid benchmark: {validation_result.summary()}")

runner = BenchmarkRunner(crs)
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

- [RFC Specification](../../docs/benchmark-spec.md): Benchmark format specification
- [Architecture](../architecture.md): Overall CRSBench architecture
- [OSS-Fuzz Interface](../../docs/ossfuzz-crs-interface.md): CRS interface specification
- [Validation Module](../validation/validation.md): Validation module design
