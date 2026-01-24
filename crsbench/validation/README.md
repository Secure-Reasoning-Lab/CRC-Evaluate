# CRSBench Validation Module

## Overview

The validation module provides robust, pure-function validation of benchmark configurations with minimal side effects. It's specifically designed to be used as a tool call by LLM agents while maintaining thread safety and providing comprehensive validation results.

## Key Design Principles

### 🔒 **Minimal Side Effects**
- **Pure functions**: No global state modifications
- **Read-only operations**: Only reads files, never writes
- **Immutable inputs**: Never modifies input data
- **Thread-safe**: Can be called concurrently by multiple agents

### 🛡️ **Robust Error Handling**
- **Graceful degradation**: Continues validation even after errors
- **Structured results**: Returns detailed validation information
- **Comprehensive reporting**: Covers syntax, schema, and logic validation
- **JSON serializable**: Results can be easily transmitted between agents

### 🎯 **Agent-Friendly**
- **Single entry point**: Simple `validate_benchmark(path)` function
- **Tool-compatible**: Perfect for LangGraph tool calls
- **Fast execution**: Optimized for quick validation cycles
- **Rich metadata**: Provides context for agent decision-making

## Core Functions

### `validate_benchmark(path: Union[str, Path]) -> ValidationResult`

Main validation function that validates a complete benchmark configuration.

```python
from crsbench.validation import validate_benchmark

# Validate by directory path
result = validate_benchmark("/path/to/benchmark")

# Validate by direct meta.yaml path
result = validate_benchmark("/path/to/benchmark/.aixcc/meta.yaml")

# Check results
if result.is_valid:
    print("✅ Benchmark is valid!")
    print(f"Found {result.metadata['total_harnesses']} harnesses")
    print(f"Found {result.metadata['total_vulns']} vulnerabilities")
    print(f"Found {result.metadata['total_povs']} POV variants")
else:
    print(f"❌ Validation failed with {result.error_count} errors")
    for error in result.errors:
        print(f"  - {error}")
```

### `validate_benchmark_from_string(yaml_content: str) -> ValidationResult`

Validates YAML content directly without file system access.

```python
from crsbench.validation import validate_benchmark_from_string

yaml_content = """
patch_exclude_list:
  - "build.sh"
  - "test/**"

full_mode:
  base_commit: "abc123def456"

harness_files:
  - name: "test_harness"
    path: "/src/project/test/harness.c"
    vulns:
      - vuln_keyword: "cpv_0"  # Must follow cpv_N pattern
        povs:
          - id: "pov_0"
            sanitizer: "address"
            error_token: "AddressSanitizer: heap-buffer-overflow"
"""

result = validate_benchmark_from_string(yaml_content)
```

## Validation Result Structure

The `ValidationResult` class provides comprehensive information about validation:

```python
class ValidationResult:
    is_valid: bool                    # Overall validation status
    issues: List[ValidationIssue]     # All issues found
    metadata: Dict[str, Any]          # Metadata about the benchmark

    # Convenience properties
    errors: List[ValidationIssue]     # Error-level issues only
    warnings: List[ValidationIssue]   # Warning-level issues only
    error_count: int                  # Number of errors
    warning_count: int                # Number of warnings

    # Methods
    def to_dict() -> Dict[str, Any]   # JSON serializable format
    def summary() -> str              # Human-readable summary
```

### Example Result Usage

```python
result = validate_benchmark("/path/to/benchmark")

# Basic validation check
if not result.is_valid:
    print(f"Validation failed: {result.summary()}")

# Detailed error analysis
for error in result.errors:
    print(f"ERROR in {error.field}: {error.message}")
    if error.context:
        print(f"  Context: {error.context}")

# Access metadata
print(f"Benchmark has {result.metadata['total_harnesses']} harnesses")
print(f"Found {result.metadata['total_vulns']} vulnerabilities")
print(f"Found {result.metadata['total_povs']} POV variants")
print(f"Using {'delta' if result.metadata['has_delta_mode'] else 'full'} mode")

# Serialize for agent communication
json_result = result.to_dict()
```

## Validation Checks

### 1. **File-Level Validation**
- ✅ `meta.yaml` file exists and is readable
- ✅ Valid YAML syntax
- ✅ Non-empty file content

### 2. **Schema Validation**
- ✅ Required fields present (`harness_files`)
- ✅ Correct data types (strings, lists, objects)
- ✅ Field value constraints (commit hash format, etc.)

### 3. **Configuration Logic**
- ✅ At least one evaluation mode (`delta_mode` or `full_mode`)
- ✅ Valid commit hash formats (7-40 hex characters)
- ✅ Unique harness names
- ✅ Valid path formats for harness files
- ✅ POV configurations are complete

### 4. **Best Practice Warnings**
- ⚠️ Missing patch exclusion patterns
- ⚠️ Large number of harnesses (>20)
- ⚠️ Complex glob patterns
- ⚠️ No vulnerability configurations

## Schema Definition

The validation uses Pydantic models for type safety and validation:

```python
class BenchmarkConfig(BaseModel):
    """Complete benchmark configuration."""

    patch_exclude_list: Optional[List[str]] = []
    delta_mode: Optional[DeltaMode] = None
    full_mode: Optional[FullMode] = None
    harness_files: List[HarnessFile]

class HarnessFile(BaseModel):
    """Harness configuration."""

    name: str
    path: str  # Absolute path in container (e.g., /src/project/test/harness.c)
    vulns: Optional[List[Vulnerability]] = []

class Vulnerability(BaseModel):
    """Vulnerability grouping POV variants by root cause."""

    vuln_keyword: str  # Maps to directory name (must follow cpv_N pattern)
    difficulty_level: Optional[int] = None  # 1-5, intrinsic difficulty
    povs: List[POV]

class POV(BaseModel):
    """Proof of Vulnerability variant."""

    id: str  # POV variant ID (e.g., pov_0, pov_1)
    sanitizer: str  # address, memory, thread, undefined, leak
    error_token: Optional[str] = None  # Optional error pattern
```

## Error Codes

The validation system uses structured error codes for consistent error handling:

### File Errors
- `FILE_NOT_FOUND`: meta.yaml file not found
- `FILE_NOT_READABLE`: Cannot read the file
- `YAML_SYNTAX_ERROR`: Invalid YAML syntax
- `EMPTY_FILE`: File is empty or contains no data

### Schema Errors
- `SCHEMA_VALIDATION_ERROR`: Pydantic validation failed
- `MISSING_REQUIRED_FIELD`: Required field missing
- `INVALID_FIELD_TYPE`: Wrong data type for field
- `INVALID_FIELD_VALUE`: Value doesn't meet constraints

### Configuration Errors
- `NO_EVALUATION_MODE`: No delta_mode or full_mode specified
- `NO_HARNESS_FILES`: No harness files configured
- `DUPLICATE_HARNESS_NAME`: Duplicate harness names
- `INVALID_COMMIT_HASH`: Malformed commit hash
- `INVALID_PATH_FORMAT`: Invalid harness path format
- `EMPTY_VULN_LIST`: No vulnerability configurations

## Usage in LangGraph Agents

The validation module is designed to be used as a tool in LangGraph workflows:

```python
from langchain.tools import Tool
from crsbench.validation import validate_benchmark

def create_validation_tool():
    """Create a validation tool for LangGraph agents."""

    def validate_tool(benchmark_path: str) -> dict:
        """Tool function for benchmark validation."""
        try:
            result = validate_benchmark(benchmark_path)
            return result.to_dict()
        except Exception as e:
            return {
                "is_valid": False,
                "error": str(e),
                "error_type": "validation_tool_error"
            }

    return Tool(
        name="validate_benchmark",
        description="Validate a benchmark configuration file for format compliance",
        func=validate_tool
    )

# Use in agent workflow
validation_tool = create_validation_tool()
result = validation_tool.run("/path/to/benchmark")

if result["is_valid"]:
    print("Benchmark is valid, proceeding with migration...")
else:
    print(f"Validation failed: {result['error_count']} errors found")
```

## Metadata Information

The validation result includes rich metadata for agent decision-making:

```python
{
    "file_path": "/path/to/benchmark/.aixcc/meta.yaml",
    "file_size": 2048,
    "yaml_valid": true,
    "schema_valid": true,
    "total_harnesses": 5,
    "total_vulns": 8,
    "total_povs": 12,
    "has_delta_mode": true,
    "has_full_mode": false,
    "patch_exclude_patterns": 8
}
```

## Performance Characteristics

- **Fast execution**: Typical validation completes in <100ms
- **Memory efficient**: Minimal memory footprint
- **Thread-safe**: No shared state between calls
- **Lazy loading**: Only loads what's needed for validation

## Error Handling Philosophy

The validation module follows a **fail-fast** approach for unexpected errors but **continues validation** for expected issues:

```python
try:
    result = validate_benchmark(path)

    # result.is_valid = False means validation found issues
    # This is expected behavior, not an exception

    if not result.is_valid:
        # Handle validation failures gracefully
        for error in result.errors:
            fix_validation_error(error)

except ValidationError as e:
    # This indicates an unexpected error in the validation process itself
    # Such as file system errors, corrupted data, etc.
    logger.error(f"Validation process failed: {e}")
    handle_validation_process_error(e)
```

## Integration Examples

### Migration Agent Integration

```python
from crsbench.validation import validate_benchmark
from crsbench.migration import migrate_benchmark

def migration_workflow(source_path: str, target_path: str):
    """Migration workflow with validation."""

    # Validate source format
    source_result = validate_benchmark(source_path)
    if not source_result.is_valid:
        return {"error": "Source benchmark is invalid", "details": source_result.to_dict()}

    # Perform migration
    migrate_benchmark(source_path, target_path)

    # Validate migrated result
    target_result = validate_benchmark(target_path)
    if not target_result.is_valid:
        return {"error": "Migration produced invalid benchmark", "details": target_result.to_dict()}

    return {"success": True, "metadata": target_result.metadata}
```

### Hint Generation Integration

```python
from crsbench.validation import validate_benchmark
from crsbench.hint_generation import generate_hints

def hint_generation_workflow(benchmark_path: str):
    """Generate hints with validation."""

    # Validate benchmark first
    result = validate_benchmark(benchmark_path)
    if not result.is_valid:
        return {"error": "Cannot generate hints for invalid benchmark"}

    # Check if benchmark has vulnerabilities
    if result.metadata["total_vulns"] == 0:
        return {"warning": "No vulnerabilities found, cannot generate hints"}

    # Generate hints based on vulnerability and POV count
    hints = generate_hints(
        benchmark_path,
        vuln_count=result.metadata["total_vulns"],
        pov_count=result.metadata["total_povs"]
    )
    return {"hints": hints, "benchmark_metadata": result.metadata}
```

## Experiment Config Validation

### `validate_experiment_config(path: Union[str, Path]) -> ValidationResult`

Validates experiment configuration files that control experiment settings.

```python
from crsbench.validation import validate_experiment_config

# Validate experiment config file
result = validate_experiment_config("/path/to/experiment-config.yaml")

if result.is_valid:
    print("✅ Experiment config is valid!")
    print(f"Trials: {result.metadata['trials']}")
    print(f"Max time: {result.metadata['max_total_time']} seconds")
    print(f"Difficulty level: {result.metadata['difficulty_level']}")
else:
    print(f"❌ Validation failed with {result.error_count} errors")
    for error in result.errors:
        print(f"  - {error}")
```

### `validate_experiment_config_from_string(yaml_content: str) -> ValidationResult`

Validates experiment configuration from YAML string.

```python
from crsbench.validation import validate_experiment_config_from_string

experiment_yaml = """
trials: 3
max_total_time: 86400
difficulty_level: 2
experiment_filestore: /tmp/experiment-data
report_filestore: /tmp/report-data
"""

result = validate_experiment_config_from_string(experiment_yaml)
```

### Experiment Config Schema

```python
class ExperimentConfig(BaseModel):
    """Experiment configuration schema."""

    trials: int                    # Number of trials (>= 1)
    max_total_time: int            # Max time in seconds per trial (>= 1)
    difficulty_level: int          # Difficulty level 0-4
    experiment_filestore: str      # Experiment data storage path
    report_filestore: str          # Report output path
```

### Experiment Config Validation Checks

- ✅ All required fields present
- ✅ `trials >= 1`
- ✅ `max_total_time >= 1`
- ✅ `difficulty_level` in range 0-4
- ✅ Filestore paths are non-empty strings

### Experiment Config Metadata

```python
{
    "trials": 3,
    "max_total_time": 86400,
    "difficulty_level": 2,
    "experiment_filestore": "/tmp/experiment-data",
    "report_filestore": "/tmp/report-data"
}
```

## Benchmark Suite Validation

### `validate_benchmark_suite(path: Union[str, Path]) -> ValidationResult`

Validates benchmark suite configuration files that define collections of benchmarks.

```python
from crsbench.validation import validate_benchmark_suite

# Validate benchmark suite config file
result = validate_benchmark_suite("/path/to/benchmark-suite.yaml")

if result.is_valid:
    print("✅ Benchmark suite is valid!")
    print(f"Suite name: {result.metadata['suite_name']}")
    print(f"Description: {result.metadata['suite_description']}")
    print(f"Total benchmarks: {result.metadata['total_benchmarks']}")
else:
    print(f"❌ Validation failed with {result.error_count} errors")
    for error in result.errors:
        print(f"  - {error}")
```

### `validate_benchmark_suite_from_string(yaml_content: str) -> ValidationResult`

Validates benchmark suite configuration from YAML string.

```python
from crsbench.validation import validate_benchmark_suite_from_string

suite_yaml = """
Name: crsbench-c
Description: A benchmark suite for evaluating C/C++ CRS
Release date: 09.23.2025
benchmark_list:
  - benchmark_id_1
  - benchmark_id_2
  - benchmark_id_3
"""

result = validate_benchmark_suite_from_string(suite_yaml)
```

### Benchmark Suite Schema

```python
class BenchmarkSuiteConfig(BaseModel):
    """Benchmark suite configuration schema."""

    Name: str                      # Unique suite identifier
    Description: str               # Suite description
    release_date: str              # Release date (MM.DD.YYYY format)
    benchmark_list: List[str]      # List of benchmark IDs
```

### Benchmark Suite Validation Checks

- ✅ All required fields present
- ✅ `Name` is non-empty
- ✅ `Description` is non-empty
- ✅ `Release date` in format MM.DD.YYYY
- ✅ `benchmark_list` has at least one benchmark ID
- ✅ No duplicate benchmark IDs
- ✅ No empty benchmark IDs

### Benchmark Suite Metadata

```python
{
    "suite_name": "crsbench-c",
    "suite_description": "A benchmark suite for evaluating C/C++ CRS",
    "release_date": "09.23.2025",
    "total_benchmarks": 3,
    "benchmark_ids": ["benchmark_id_1", "benchmark_id_2", "benchmark_id_3"]
}
```

### Error Codes for Experiment and Benchmark Suite

**Experiment Config Errors:**
- `INVALID_TRIALS`: trials must be >= 1
- `INVALID_TIME_LIMIT`: max_total_time must be >= 1
- `INVALID_DIFFICULTY_LEVEL`: difficulty_level must be 0-4
- `INVALID_DIRECTORY_PATH`: filestore paths must be valid

**Benchmark Suite Errors:**
- `INVALID_SUITE_NAME`: Name cannot be empty
- `INVALID_RELEASE_DATE`: Release date format invalid
- `EMPTY_BENCHMARK_LIST`: benchmark_list must have at least one entry
- `DUPLICATE_BENCHMARK_ID`: Duplicate benchmark IDs found

## Best Practices

### For Agent Developers
1. **Always check `is_valid`** before proceeding with other operations
2. **Use `to_dict()`** for JSON serialization between agents
3. **Check metadata** for decision-making context
4. **Handle `ValidationError`** exceptions for process errors
5. **Use error codes** for programmatic error handling

### For Validation Users
1. **Validate early** in workflows to catch issues quickly
2. **Pay attention to warnings** - they indicate potential problems
3. **Use metadata** to understand benchmark characteristics
4. **Check file paths** are resolved correctly
5. **Test with invalid inputs** to ensure proper error handling

This validation module provides a solid foundation for ensuring benchmark quality while being safe and efficient for agent-based workflows.