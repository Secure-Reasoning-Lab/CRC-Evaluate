# CRSBench Evaluation Module

The evaluation module provides functionality to run benchmark evaluations against CRS (Cyber Reasoning System) implementations.

## Overview

The evaluation module consists of several key components:

- **BenchmarkRunner**: Orchestrates the entire evaluation process
- **CRSExecutor**: Abstract interface for CRS implementations
- **StubCRSExecutor**: Testing implementation that simulates CRS behavior
- **EvaluationReport**: Comprehensive results with detailed metrics
- **ResultCollector**: Manages result collection during evaluation

## Quick Start

```python
from pathlib import Path
from crsbench.evaluation import BenchmarkRunner, StubCRSExecutor

# Create CRS executor
crs_executor = StubCRSExecutor()

# Configure simulation parameters
crs_config = {
    "simulation_delay": 0.1,  # Execution time per harness
    "success_rate": 0.8       # POV detection rate (0.0-1.0)
}

# Create runner and evaluate
runner = BenchmarkRunner(crs_executor)
result = runner.run_benchmark(
    benchmark_path=Path("test_benchmark"),
    mode="auto",  # "auto", "delta", or "full"
    crs_config=crs_config
)

# Access results
print(f"POVs found: {result.report.povs_found}/{result.report.total_povs}")
print(f"Success rate: {result.report.success_rate:.1%}")

# Save results
result.report.save_json(Path("results.json"))
result.report.save_yaml(Path("results.yaml"))
```

## Benchmark Format

Benchmarks must contain a `meta.yaml` file with the following structure:

```yaml
patch_exclude_list:
  - "build.sh"
  - "test/**"

# Evaluation modes (at least one required)
full_mode:
  base_commit: "abc123def456789"

delta_mode:
  base_commit: "abc123def456789"
  ref_commit: "def456789abc123"

# Harness configurations
harness_files:
  - name: "test_harness_1"
    path: "/src/project/test_harness_1.c"
    vulns:
      - vuln_keyword: "buffer_overflow_test"
        povs:
          - id: "pov_0"
            sanitizer: "address"
            error_token: "AddressSanitizer: heap-buffer-overflow"

  - name: "test_harness_2"
    path: "/src/project/harness/test.c"
    vulns:
      - vuln_keyword: "use_after_free_test"
        povs:
          - id: "pov_0"
            sanitizer: "address"
            error_token: "AddressSanitizer: heap-use-after-free"
```

## Path Format

- Use absolute paths: `/src/project/...` for harness files
- Relative paths (`./...`) also supported for flexibility

## Evaluation Modes

### Auto Mode
Automatically selects the best available mode:
1. Delta mode if available
2. Full mode as fallback

### Delta Mode
Compares two git commits to evaluate CRS on specific changes:
- `base_commit`: Starting point
- `ref_commit`: Target commit with changes

### Full Mode
Evaluates the complete codebase at a specific commit:
- `base_commit`: Commit to evaluate

## Results Structure

The evaluation produces comprehensive results including:

- **Summary metrics**: POVs found/missed/error, success rates
- **Harness results**: Individual harness execution details
- **POV results**: Detailed status for each proof of vulnerability
- **Timing information**: Execution times and timestamps
- **Configuration**: CRS config and commit information

## Implementing Custom CRS Executors

Extend the `CRSExecutor` abstract base class:

```python
from crsbench.evaluation import CRSExecutor, CRSResult
from pathlib import Path

class MyCRSExecutor(CRSExecutor):
    def configure_crs(self, config):
        # Configure your CRS with provided parameters
        self.my_crs.configure(**config)

    def run_crs(self, benchmark_path: Path, harness: HarnessFile,
                base_commit: str, ref_commit=None):
        # Run your CRS implementation
        # Return CRSResult with execution details
        pass

    def process_pov_results(self, crs_result: CRSResult, harness: HarnessFile):
        # Analyze CRS output to determine POV detection status
        # Return List[POVResult]
        pass
```

## Error Handling

The module provides comprehensive error handling:

- **EvaluationError**: Raised for evaluation-specific failures
- **ValidationError**: Configuration validation failures
- Graceful handling of individual harness failures
- Detailed error reporting in results

## Example Script

See `example_evaluation.py` for a complete working example that demonstrates:
- Proper error handling
- Multiple evaluation runs with different parameters
- Result formatting and output
- YAML and JSON export

## Integration Notes

- The evaluation module integrates with the existing validation module
- Results are compatible with CI/CD pipelines via JSON/YAML export
- Logging is configurable for different verbosity levels
- Thread-safe for concurrent evaluations (when using different CRS instances)