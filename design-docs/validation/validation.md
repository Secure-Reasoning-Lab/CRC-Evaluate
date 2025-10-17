# Validation Module Design

This document provides detailed implementation documentation for the `crsbench.validation` module.

## Purpose

The validation module provides pure-function validation of benchmark configurations with minimal side effects. It's specifically designed to be safe for use as a tool call by LLM agents while maintaining thread safety and providing comprehensive validation results.

## Architecture Overview

### Module Structure

```
crsbench/validation/
├── __init__.py              # Public API exports
├── schemas.py               # Pydantic models for type-safe validation
├── format_validator.py      # Core validation logic
├── errors.py                # Error types and codes
└── README.md                # User-facing documentation
```

### Design Philosophy

1. **Pure Functions**: No global state modifications, only reads and returns results
2. **Minimal Side Effects**: Read-only operations, never writes files
3. **Thread-Safe**: Can be called concurrently by multiple agents
4. **Graceful Degradation**: Continues validation even after errors to report all issues
5. **Agent-Friendly**: Simple API, JSON serializable results, fast execution

## Data Models (schemas.py)

### Model Hierarchy

```
BenchmarkConfig
    ├── patch_exclude_list: List[str]
    ├── delta_mode: Optional[DeltaMode]
    ├── full_mode: Optional[FullMode]
    └── harness_files: List[HarnessFile]
            ├── name: str
            ├── path: str (absolute or relative)
            └── vulns: Optional[List[Vulnerability]]
                    ├── vuln_keyword: str
                    ├── difficulty_level: Optional[int]
                    └── povs: List[POV]
                            ├── id: str
                            ├── sanitizer: str
                            └── error_token: Optional[str]
```

### Why This Structure?

#### Nested Vulnerability → POV Structure

**Design Decision**: Use nested `vulns → povs` instead of flat `povs` list.

**Rationale**:
- Groups POV variants by root cause (vulnerability keyword)
- Multiple POVs can trigger the same underlying bug through different code paths
- Patches are applied at vulnerability level, fixing all POV variants
- Matches directory structure: `[vuln-keyword]/[pov-variant-id]/`

**Example**:
```yaml
vulns:
  - vuln_keyword: "buffer_overflow"     # Root cause
    povs:
      - id: "pov_0"                     # Variant 1: ASAN detection
        sanitizer: "address"
      - id: "pov_1"                     # Variant 2: UBSAN detection
        sanitizer: "undefined"
```

#### Optional error_token

**Design Decision**: Make `error_token` optional in POV configuration.

**Rationale**:
- error_token is for descriptive/documentation purposes
- Some POVs may not have predictable error strings
- Deduplication should use more robust methods than simple string matching
- Allows flexibility for different vulnerability detection strategies

#### Absolute Paths (No $REPO/$PROJECT)

**Design Decision**: Remove `$REPO/` and `$PROJECT/` variable support.

**Rationale**:
- Simplifies implementation (no variable substitution)
- More explicit and less error-prone
- Docker containers use absolute paths anyway (`/src/project/...`)
- Relative paths (`./...`) still supported for flexibility
- Clean break from old format (no backward compatibility)

**Migration**: Old `$REPO/test/harness.c` → New `/src/project/test/harness.c`

### POV Model

```python
class POV(BaseModel):
    id: str                          # POV variant ID (e.g., "pov_0")
    sanitizer: str                   # address, memory, undefined, thread, leak
    error_token: Optional[str]       # Optional error pattern
```

**Validation Rules**:
- `id` cannot be empty
- `sanitizer` must be one of valid types
- `error_token` if provided cannot be empty string (use None instead)
- Within a vulnerability, POV IDs must be unique

### Vulnerability Model

```python
class Vulnerability(BaseModel):
    vuln_keyword: str                # Maps to directory name
    difficulty_level: Optional[int]  # 1-5, intrinsic difficulty
    povs: List[POV]                  # At least one POV required
```

**Validation Rules**:
- `vuln_keyword` cannot be empty
- `difficulty_level` if provided must be 1-5
- Must have at least one POV
- POV IDs must be unique within vulnerability

### HarnessFile Model

```python
class HarnessFile(BaseModel):
    name: str                        # Harness identifier
    path: str                        # Absolute or relative path
    vulns: Optional[List[Vulnerability]]  # Optional vulnerabilities
```

**Validation Rules**:
- `name` cannot be empty
- `path` must be absolute (`/...`) or relative (`./...`)
- No duplicate harness names in same benchmark
- Harnesses without `vulns` are allowed (distractor harnesses)

### BenchmarkConfig Model

```python
class BenchmarkConfig(BaseModel):
    patch_exclude_list: Optional[List[str]]
    delta_mode: Optional[DeltaMode]
    full_mode: Optional[FullMode]
    harness_files: List[HarnessFile]
```

**Validation Rules**:
- Must have at least one harness file
- Must have at least one evaluation mode (delta_mode or full_mode)
- Harness names must be unique
- Commit hashes must be valid (7-40 hex characters)

## Validation Pipeline (format_validator.py)

### Validation Flow

```
Input (Path or String)
    ↓
1. File Resolution
    └→ Locate meta.yaml file
    └→ Check file exists and is readable
    ↓
2. YAML Parsing
    └→ Parse YAML syntax
    └→ Check for empty content
    ↓
3. Schema Validation
    └→ Validate against Pydantic models
    └→ Check field types and constraints
    ↓
4. Logic Validation
    └→ Check evaluation modes
    └→ Validate harness configurations
    └→ Check commit hashes
    └→ Verify vulnerability configurations
    ↓
5. Metadata Generation
    └→ Count harnesses, vulns, POVs
    └→ Record mode configurations
    └→ Track patch exclusions
    ↓
6. Warning Generation
    └→ Check for missing exclusions
    └→ Warn about many harnesses
    └→ Detect complex glob patterns
    ↓
Output (ValidationResult)
```

### Key Functions

#### validate_benchmark(path: Union[str, Path]) -> ValidationResult

Main validation entry point for file-based validation.

**Process**:
1. Resolve path to meta.yaml
2. Check file accessibility
3. Load and parse YAML
4. Validate through full pipeline
5. Return comprehensive results

**Error Handling**:
- File not found → Add error, return early
- Not readable → Add error, return early
- YAML syntax error → Add error, return early
- Schema errors → Add all errors, continue
- Logic errors → Add all errors, continue

#### validate_benchmark_from_string(yaml_content: str) -> ValidationResult

Pure validation from string (no file system access).

**Use Cases**:
- Testing validation logic
- Agent-generated YAML validation
- Inline configuration validation
- Pre-save validation

### Metadata Generation

```python
def _generate_metadata(config: BenchmarkConfig, result: ValidationResult):
    total_vulns = sum(len(h.vulns or []) for h in config.harness_files)
    total_povs = sum(
        len(vuln.povs)
        for h in config.harness_files
        for vuln in (h.vulns or [])
    )

    result.metadata.update({
        "total_harnesses": len(config.harness_files),
        "total_vulns": total_vulns,
        "total_povs": total_povs,
        "has_delta_mode": config.delta_mode is not None,
        "has_full_mode": config.full_mode is not None,
        "patch_exclude_patterns": len(config.patch_exclude_list or [])
    })
```

**Why Two Counts?**:
- `total_vulns`: Number of unique vulnerabilities (root causes)
- `total_povs`: Number of POV variants across all vulnerabilities
- Helps understand benchmark complexity and coverage

## Error Handling (errors.py)

### Error Classification

#### Severity Levels

```python
class ValidationSeverity(str, Enum):
    ERROR = "error"      # Blocks benchmark usage
    WARNING = "warning"  # Potential issues
    INFO = "info"        # Informational messages
```

#### Error Codes

**File-Level**:
- `FILE_NOT_FOUND`: meta.yaml doesn't exist
- `FILE_NOT_READABLE`: Cannot read file
- `YAML_SYNTAX_ERROR`: Invalid YAML syntax
- `EMPTY_FILE`: No content in file

**Schema-Level**:
- `SCHEMA_VALIDATION_ERROR`: Pydantic validation failed
- `MISSING_REQUIRED_FIELD`: Required field missing
- `INVALID_FIELD_TYPE`: Wrong data type
- `INVALID_FIELD_VALUE`: Value constraint violation

**Configuration-Level**:
- `NO_EVALUATION_MODE`: No delta_mode or full_mode
- `NO_HARNESS_FILES`: No harnesses configured
- `DUPLICATE_HARNESS_NAME`: Duplicate names
- `INVALID_COMMIT_HASH`: Malformed commit hash
- `INVALID_PATH_FORMAT`: Invalid harness path
- `EMPTY_VULN_LIST`: No vulnerabilities configured (warning)

### ValidationResult Structure

```python
class ValidationResult(BaseModel):
    is_valid: bool                    # Overall status
    issues: List[ValidationIssue]     # All issues found
    metadata: Dict[str, Any]          # Metadata about benchmark

    # Convenience properties
    errors: List[ValidationIssue]     # Errors only
    warnings: List[ValidationIssue]   # Warnings only
    error_count: int
    warning_count: int

    # Methods
    def add_error(...)                # Add error
    def add_warning(...)              # Add warning
    def add_info(...)                 # Add info
    def to_dict() -> Dict             # Serialize
    def summary() -> str              # Human-readable
```

### Graceful Degradation

When validation fails, the validator:
1. **Doesn't crash**: Returns ValidationResult with is_valid=False
2. **Reports all issues**: Continues checking to find all problems
3. **Provides context**: Includes field names, line numbers, error details
4. **Generates metadata**: Even for invalid configs (partial metadata)

**Example**:
```python
result = validate_benchmark(path)
if not result.is_valid:
    # Still get useful information
    print(f"Found {result.error_count} errors")
    print(f"Has {result.metadata['total_harnesses']} harnesses")
    for error in result.errors:
        print(f"  {error.field}: {error.message}")
```

## Design Decisions

### Why Pure Functions?

**Problem**: LLM agents need safe tools that don't corrupt state.

**Solution**: Validation functions are pure:
- Only read inputs (files, strings)
- Never modify inputs or global state
- Always return same output for same input
- No hidden dependencies

**Benefits**:
- Safe for concurrent agent calls
- Easy to test
- Predictable behavior
- Cacheable results

### Why Pydantic Models?

**Problem**: Need type safety and runtime validation.

**Solution**: Use Pydantic v2 for all data models.

**Benefits**:
- Type hints enforced at runtime
- Automatic validation on construction
- JSON serialization built-in
- Clear error messages
- IDE autocomplete support

**Example**:
```python
# Invalid config caught immediately
try:
    pov = POV(id="pov_0", sanitizer="invalid_type")
except ValidationError as e:
    print(e)  # Clear error about invalid sanitizer
```

### Why Nested Structure?

**Problem**: Need to group related POV variants.

**Solution**: Nest POVs under vulnerabilities.

**Benefits**:
- Matches directory structure
- Clear root cause grouping
- Patches apply to all variants
- Better organization for many POVs

**Alternative Considered**: Flat POV list with vuln_keyword field
**Rejected Because**: Harder to ensure all POVs for a vulnerability share same vuln_keyword, less intuitive structure

### Why Optional error_token?

**Problem**: Not all POVs have predictable error strings.

**Solution**: Make error_token optional.

**Benefits**:
- Flexibility for different detection methods
- Doesn't force artificial error patterns
- Still useful for documentation
- Allows gradual migration

**Trade-off**: Deduplication must use more robust methods than string matching.

### Why No Backward Compatibility?

**Problem**: Old format uses `$REPO/` variables and flat POV structure.

**Solution**: Clean break, force migration.

**Benefits**:
- Simpler code (no variable substitution)
- Clearer data model
- Faster validation
- No legacy edge cases

**Trade-off**: Requires migration effort (but migration tools provided).

## Testing Strategy

### Unit Tests Location

**File**: `tests/test_validation.py`

**IMPORTANT**: When updating the validation module, you **MUST** update `tests/test_validation.py` to reflect and test the changes.

### Test Categories

#### 1. Schema Validation Tests

Test Pydantic model validation:
```python
def test_pov_model_valid():
    pov = POV(id="pov_0", sanitizer="address")
    assert pov.id == "pov_0"

def test_pov_model_invalid_sanitizer():
    with pytest.raises(ValidationError):
        POV(id="pov_0", sanitizer="invalid")

def test_pov_optional_error_token():
    pov = POV(id="pov_0", sanitizer="address")
    assert pov.error_token is None
```

#### 2. Format Validator Tests

Test validation pipeline:
```python
def test_validate_valid_benchmark():
    yaml_content = """
    full_mode:
      base_commit: "abc123"
    harness_files:
      - name: "test"
        path: "/src/test.c"
        vulns:
          - vuln_keyword: "buffer_overflow"
            povs:
              - id: "pov_0"
                sanitizer: "address"
    """
    result = validate_benchmark_from_string(yaml_content)
    assert result.is_valid
    assert result.metadata["total_vulns"] == 1
    assert result.metadata["total_povs"] == 1
```

#### 3. Error Handling Tests

Test graceful degradation:
```python
def test_invalid_yaml_syntax():
    yaml_content = "invalid: yaml: syntax:"
    result = validate_benchmark_from_string(yaml_content)
    assert not result.is_valid
    assert result.error_count > 0

def test_missing_required_field():
    yaml_content = """
    full_mode:
      base_commit: "abc123"
    # Missing harness_files
    """
    result = validate_benchmark_from_string(yaml_content)
    assert not result.is_valid
    assert any("harness_files" in str(e) for e in result.errors)
```

#### 4. Path Validation Tests

Test new path requirements:
```python
def test_absolute_path_valid():
    harness = HarnessFile(name="test", path="/src/project/test.c")
    assert harness.path == "/src/project/test.c"

def test_repo_variable_invalid():
    with pytest.raises(ValidationError):
        HarnessFile(name="test", path="$REPO/test.c")

def test_relative_path_valid():
    harness = HarnessFile(name="test", path="./test/harness.c")
    assert harness.path == "./test/harness.c"
```

#### 5. Metadata Generation Tests

Test metadata calculation:
```python
def test_metadata_counts():
    config = create_test_config()  # Helper function
    result = ValidationResult(is_valid=True)
    _generate_metadata(config, result)

    assert result.metadata["total_harnesses"] == 2
    assert result.metadata["total_vulns"] == 3
    assert result.metadata["total_povs"] == 5
```

#### 6. Integration Tests

Test with actual meta.yaml files:
```python
def test_validate_meta_example():
    result = validate_benchmark("docs/meta-example.yaml")
    assert result.is_valid
    assert result.metadata["total_harnesses"] == 2
    assert result.metadata["total_vulns"] == 2
    assert result.metadata["total_povs"] == 3
```

### Test Maintenance Guidelines

When you update the validation module:

1. **Schema Changes** → Update:
   - Model validation tests
   - Test fixtures
   - Expected error messages

2. **Validation Logic Changes** → Update:
   - Format validator tests
   - Error handling tests
   - Edge case tests

3. **New Fields/Models** → Add:
   - Tests for new field validation
   - Tests for optional/required behavior
   - Tests for new error codes

4. **Bug Fixes** → Add:
   - Regression test for the bug
   - Test for the fix
   - Document the edge case

### Running Tests

```bash
# Run all validation tests
pytest tests/test_validation.py -v

# Run specific test
pytest tests/test_validation.py::test_pov_model_valid -v

# Run with coverage
pytest tests/test_validation.py --cov=crsbench.validation --cov-report=html

# Run specific test category
pytest tests/test_validation.py -k "schema" -v
```

## Performance Characteristics

### Benchmarks

Typical validation performance:
- File access: <10ms
- YAML parsing: <20ms
- Schema validation: <50ms
- Logic validation: <20ms
- **Total: <100ms** for typical benchmark

### Memory Usage

- Minimal memory footprint (~5MB)
- No caching or persistent state
- Immediate garbage collection

### Scalability

Scales linearly with:
- Number of harnesses
- Number of vulnerabilities
- Number of POV variants

No performance degradation with:
- Large patch exclusion lists
- Complex glob patterns
- Nested directory structures

## Integration with Other Modules

### Migration Module

```python
from crsbench.validation import validate_benchmark

def migrate_benchmark(old_path, new_path):
    # Convert old format to new
    convert_format(old_path, new_path)

    # Validate migrated benchmark
    result = validate_benchmark(new_path)
    if not result.is_valid:
        raise MigrationError(f"Migrated benchmark invalid: {result.errors}")
```

### Evaluation Module

```python
from crsbench.validation import validate_benchmark

def load_benchmark(path):
    # Validate before loading
    result = validate_benchmark(path)
    if not result.is_valid:
        raise EvaluationError(f"Invalid benchmark: {result.summary()}")

    # Use metadata for optimization
    if result.metadata["total_povs"] > 100:
        enable_parallel_processing()
```

### Builder Module

```python
from crsbench.validation import validate_benchmark

def build_benchmark_image(benchmark_path):
    # Validate before building
    result = validate_benchmark(benchmark_path)
    if not result.is_valid:
        logger.error(f"Cannot build invalid benchmark")
        return False

    # Use validated config for build
    proceed_with_build(benchmark_path)
```

## Common Pitfalls

### 1. Modifying Input Data

**Wrong**:
```python
def validate(data: dict):
    data["validated"] = True  # Modifies input!
    return ValidationResult(is_valid=True)
```

**Right**:
```python
def validate(data: dict) -> ValidationResult:
    result = ValidationResult(is_valid=True)
    result.metadata["input_size"] = len(data)
    return result  # Input unchanged
```

### 2. Hidden Dependencies

**Wrong**:
```python
_global_config = {}

def validate(path: str):
    _global_config["last_path"] = path  # Hidden state!
    return validate_internal(path)
```

**Right**:
```python
def validate(path: str) -> ValidationResult:
    result = ValidationResult(is_valid=True)
    result.metadata["file_path"] = str(path)
    return result  # No hidden state
```

### 3. Assuming Paths Exist

**Wrong**:
```python
def validate(path: Path):
    content = path.read_text()  # Crashes if missing!
    return validate_yaml(content)
```

**Right**:
```python
def validate(path: Path) -> ValidationResult:
    result = ValidationResult(is_valid=True)
    if not path.exists():
        result.add_error("FILE_NOT_FOUND", f"File not found: {path}")
        return result
    # Continue validation...
```

### 4. Swallowing Errors

**Wrong**:
```python
def validate(data: dict):
    try:
        config = BenchmarkConfig(**data)
        return ValidationResult(is_valid=True)
    except Exception:
        return ValidationResult(is_valid=False)  # Lost error info!
```

**Right**:
```python
def validate(data: dict) -> ValidationResult:
    result = ValidationResult(is_valid=True)
    try:
        config = BenchmarkConfig(**data)
    except ValidationError as e:
        for error in e.errors():
            result.add_error("SCHEMA_ERROR", str(error))
    return result  # All errors captured
```

## Future Enhancements

### Planned Improvements

1. **JSON Schema Export**: Generate JSON Schema from Pydantic models
2. **Validation Levels**: Quick/Standard/Thorough validation modes
3. **Custom Validators**: Plugin system for domain-specific validation
4. **Validation Caching**: Cache validation results for unchanged files
5. **Incremental Validation**: Validate only changed parts
6. **Better Error Messages**: More context and suggestions

### Extension Points

- Custom validators for specific vulnerability types
- Domain-specific sanitizer validation
- Project-specific path validation rules
- Custom metadata extractors

## References

- [RFC Specification](../docs/benchmark-spec.md): Full benchmark format specification
- [Module README](../crsbench/validation/README.md): User-facing documentation
- [Architecture](../architecture.md): Overall CRSBench architecture
- [Pydantic Documentation](https://docs.pydantic.dev/): Pydantic v2 docs
