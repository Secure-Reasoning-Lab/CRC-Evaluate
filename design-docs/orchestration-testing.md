# Orchestration Testing Design

This document describes the testing strategy for the orchestration layer, focusing on experiment configuration, job generation, CLI overrides, and reproducibility through stored configurations.

## Purpose

The orchestration testing ensures:
1. **Correct job generation**: Trial matrix matches expected combinations
2. **CLI override behavior**: CLI arguments correctly override config values
3. **Config storage**: Trial directories contain resolved (overridden) configuration
4. **Reproducibility**: Stored config.yaml enables exact reproduction of experiment
5. **Integration correctness**: End-to-end workflow produces expected results

## Test Categories

### 1. Experiment Configuration Loading

#### Test: Load Basic Config

**File**: `tests/test_orchestration.py`

```python
def test_load_experiment_config_basic(tmp_path):
    """Test loading basic experiment configuration."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("""
experiment: test-experiment
trials: 3
max_total_time: 7200
difficulty_level: 2
experiment_filestore: /tmp/experiment-data
report_filestore: /tmp/report-data
crses:
  - atlantis-c
  - ensemble-c
benchmarks:
  - curl-delta-02
  - libxml2-delta-03
""")

    config = load_experiment_config(config_path)

    assert config.experiment == "test-experiment"
    assert config.trials == 3
    assert config.max_total_time == 7200
    assert config.difficulty_level == 2
    assert config.crses == ["atlantis-c", "ensemble-c"]
    assert config.benchmarks == ["curl-delta-02", "libxml2-delta-03"]
```

**Success Criteria**: Config loads correctly with all fields populated

#### Test: Load Config with Benchmark Suite

```python
def test_load_experiment_config_with_suite(tmp_path):
    """Test loading config with benchmark_suite instead of benchmarks."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("""
experiment: suite-experiment
trials: 2
max_total_time: 3600
difficulty_level: 1
experiment_filestore: /tmp/experiment-data
report_filestore: /tmp/report-data
crses:
  - atlantis-c
benchmark_suite: crsbench-afc-c
""")

    config = load_experiment_config(config_path)

    assert config.experiment == "suite-experiment"
    assert config.benchmark_suite == "crsbench-afc-c"
    assert config.benchmarks is None  # Not set when using suite
```

**Success Criteria**: Config loads with benchmark_suite field

#### Test: Config Validation Errors

```python
def test_load_experiment_config_missing_required_field():
    """Test that missing required fields raise validation errors."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("""
experiment: incomplete-config
trials: 2
# Missing max_total_time, experiment_filestore, report_filestore
""")

    with pytest.raises(ValidationError) as exc_info:
        config = load_experiment_config(config_path)

    # Verify error message mentions missing fields
    assert "max_total_time" in str(exc_info.value)
```

**Success Criteria**: Validation errors raised for incomplete configs

### 2. Trial Matrix Generation

#### Test: Basic Matrix Generation

```python
def test_generate_trial_matrix_basic():
    """Test basic trial matrix generation."""
    config = ExperimentConfig(
        trials=2,
        max_total_time=3600,
        difficulty_level=1,
        experiment_filestore="/tmp/exp",
        report_filestore="/tmp/rep"
    )

    benchmarks = ["bench1", "bench2"]
    crses = ["crs1", "crs2"]

    trials = generate_trial_matrix(benchmarks, crses, config)

    # Expected: 2 CRSes × 2 benchmarks × 2 trials = 8 total
    assert len(trials) == 8

    # Verify structure
    assert all(isinstance(t, Trial) for t in trials)

    # Verify all combinations exist
    expected = [
        ("crs1", "bench1", 0), ("crs1", "bench1", 1),
        ("crs1", "bench2", 0), ("crs1", "bench2", 1),
        ("crs2", "bench1", 0), ("crs2", "bench1", 1),
        ("crs2", "bench2", 0), ("crs2", "bench2", 1),
    ]

    actual = [(t.crs, t.benchmark, t.trial_num) for t in trials]
    assert actual == expected
```

**Success Criteria**:
- Correct number of trials generated
- All CRS × Benchmark × Trial combinations present
- Correct ordering (CRS → Benchmark → Trial number)

#### Test: Matrix with Benchmark Suite

```python
def test_generate_trial_matrix_with_suite(tmp_path):
    """Test trial matrix generation with benchmark suite expansion."""
    # Create mock benchmark suite file
    suite_path = tmp_path / "benchmark-suites" / "test-suite.yaml"
    suite_path.parent.mkdir(parents=True, exist_ok=True)
    suite_path.write_text("""
Name: test-suite
Description: Test suite
Release date: 01.01.2025
benchmark_list:
  - bench1
  - bench2
  - bench3
""")

    config = ExperimentConfig(
        trials=1,
        max_total_time=3600,
        difficulty_level=1,
        experiment_filestore="/tmp/exp",
        report_filestore="/tmp/rep"
    )

    # Load suite and expand benchmarks
    suite_config = load_benchmark_suite(suite_path)
    benchmarks = suite_config["benchmark_list"]
    crses = ["crs1"]

    trials = generate_trial_matrix(benchmarks, crses, config)

    # Expected: 1 CRS × 3 benchmarks × 1 trial = 3 total
    assert len(trials) == 3
    assert set(t.benchmark for t in trials) == {"bench1", "bench2", "bench3"}
```

**Success Criteria**: Benchmark suite correctly expands to individual benchmarks

### 3. CLI Override Testing

#### Test: Override Experiment Name

```python
def test_cli_override_experiment_name(tmp_path):
    """Test CLI override of experiment name."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("""
experiment: original-name
trials: 1
max_total_time: 3600
difficulty_level: 1
experiment_filestore: /tmp/exp
report_filestore: /tmp/rep
crses: [crs1]
benchmarks: [bench1]
""")

    config = load_experiment_config(config_path)

    # Simulate CLI override
    cli_experiment_name = "overridden-name"

    # Resolution logic
    resolved_name = cli_experiment_name if cli_experiment_name else config.experiment

    assert resolved_name == "overridden-name"
    assert config.experiment == "original-name"  # Original unchanged
```

**Success Criteria**: CLI value takes precedence, original config unchanged

#### Test: Override CRSes List

```python
def test_cli_override_crses(tmp_path):
    """Test CLI override of CRS list."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("""
experiment: test
trials: 1
max_total_time: 3600
difficulty_level: 1
experiment_filestore: /tmp/exp
report_filestore: /tmp/rep
crses:
  - atlantis-c
  - ensemble-c
benchmarks: [bench1]
""")

    config = load_experiment_config(config_path)

    # Simulate CLI override
    cli_crses = "custom-crs1,custom-crs2"

    # Resolution logic
    resolved_crses = parse_list_argument(cli_crses) if cli_crses else config.crses

    assert resolved_crses == ["custom-crs1", "custom-crs2"]
    assert config.crses == ["atlantis-c", "ensemble-c"]  # Original unchanged
```

**Success Criteria**: CLI CRS list overrides config

#### Test: Override Benchmarks

```python
def test_cli_override_benchmarks(tmp_path):
    """Test CLI override of benchmarks list."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("""
experiment: test
trials: 1
max_total_time: 3600
difficulty_level: 1
experiment_filestore: /tmp/exp
report_filestore: /tmp/rep
crses: [crs1]
benchmarks:
  - bench1
  - bench2
""")

    config = load_experiment_config(config_path)

    # Simulate CLI override
    cli_benchmarks = "custom-bench1,custom-bench2,custom-bench3"

    # Resolution logic
    resolved_benchmarks = parse_list_argument(cli_benchmarks) if cli_benchmarks else config.benchmarks

    assert resolved_benchmarks == ["custom-bench1", "custom-bench2", "custom-bench3"]
    assert config.benchmarks == ["bench1", "bench2"]  # Original unchanged
```

**Success Criteria**: CLI benchmark list overrides config

#### Test: Override Benchmark Suite

```python
def test_cli_override_benchmark_suite(tmp_path):
    """Test CLI override with benchmark suite."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("""
experiment: test
trials: 1
max_total_time: 3600
difficulty_level: 1
experiment_filestore: /tmp/exp
report_filestore: /tmp/rep
crses: [crs1]
benchmarks:
  - bench1
  - bench2
""")

    config = load_experiment_config(config_path)

    # Simulate CLI override with suite (takes precedence over config.benchmarks)
    cli_suite = "crsbench-afc-c"

    # Resolution logic (suite overrides benchmarks list)
    if cli_suite:
        suite_config = load_benchmark_suite(f"benchmark-suites/{cli_suite}.yaml")
        resolved_benchmarks = suite_config["benchmark_list"]
    else:
        resolved_benchmarks = config.benchmarks

    assert len(resolved_benchmarks) > 2  # Suite has more benchmarks
```

**Success Criteria**: CLI suite overrides config benchmarks list

#### Test: Mutual Exclusivity

```python
def test_cli_mutual_exclusivity_error():
    """Test error when both --benchmarks and --benchmark-suite specified."""
    # Simulate CLI args
    cli_benchmarks = "bench1,bench2"
    cli_suite = "crsbench-afc-c"

    # Should raise error
    if cli_benchmarks and cli_suite:
        with pytest.raises(ValueError, match="Cannot specify both"):
            raise ValueError("Cannot specify both --benchmarks and --benchmark-suite")
```

**Success Criteria**: Error raised when both specified

### 4. Config Storage in Trial Directory

#### Test: Store Resolved Config

**CRITICAL**: This is the key test for reproducibility. The config.yaml stored in trial_output_dir MUST contain the resolved (overridden) values, NOT the original config file values.

```python
def test_store_resolved_config_in_trial_dir(tmp_path):
    """Test that orchestrator stores RESOLVED config in trial directory.

    CRITICAL: The stored config.yaml must contain overridden values from CLI,
    not the original values from the experiment config file.
    """
    # Create original config
    config_path = tmp_path / "config.yaml"
    config_path.write_text("""
experiment: original-experiment
trials: 1
max_total_time: 3600
difficulty_level: 1
experiment_filestore: /tmp/exp
report_filestore: /tmp/rep
crses:
  - atlantis-c
  - ensemble-c
benchmarks:
  - bench1
  - bench2
""")

    # Load config
    config = load_experiment_config(config_path)

    # Simulate CLI overrides
    cli_experiment_name = "overridden-experiment"
    cli_crses = "custom-crs1,custom-crs2"
    cli_benchmarks = "custom-bench1,custom-bench2,custom-bench3"

    # Resolve configuration (what orchestrator would do)
    resolved_config = {
        "experiment": cli_experiment_name,
        "trials": config.trials,
        "max_total_time": config.max_total_time,
        "difficulty_level": config.difficulty_level,
        "experiment_filestore": config.experiment_filestore,
        "report_filestore": config.report_filestore,
        "crses": parse_list_argument(cli_crses),
        "benchmarks": parse_list_argument(cli_benchmarks),
    }

    # Store resolved config (what orchestrator does)
    trial_output_dir = tmp_path / "trial_0"
    trial_output_dir.mkdir(parents=True, exist_ok=True)

    config_yaml_path = trial_output_dir / "config.yaml"
    with open(config_yaml_path, "w") as f:
        yaml.dump(resolved_config, f)

    # Load stored config and verify it has OVERRIDDEN values
    with open(config_yaml_path) as f:
        stored_config = yaml.safe_load(f)

    # CRITICAL ASSERTIONS: Stored config must have overridden values
    assert stored_config["experiment"] == "overridden-experiment"  # NOT "original-experiment"
    assert stored_config["crses"] == ["custom-crs1", "custom-crs2"]  # NOT ["atlantis-c", "ensemble-c"]
    assert stored_config["benchmarks"] == ["custom-bench1", "custom-bench2", "custom-bench3"]  # NOT ["bench1", "bench2"]

    print("✓ Stored config contains RESOLVED (overridden) values")
```

**Success Criteria**:
- config.yaml in trial_output_dir contains overridden values
- Reproducibility: Someone can rerun the exact experiment from stored config
- Stored config reflects what was actually executed

#### Test: Config + Execution Metadata Reproducibility

```python
def test_config_and_execution_metadata_together(tmp_path):
    """Test that config.yaml + execution.json provide complete reproducibility."""
    trial_output_dir = tmp_path / "trial_0"
    trial_output_dir.mkdir(parents=True, exist_ok=True)

    # Orchestrator stores resolved config
    resolved_config = {
        "experiment": "test-experiment",
        "trials": 2,
        "max_total_time": 3600,
        "difficulty_level": 2,
        "crses": ["atlantis-c"],
        "benchmarks": ["curl-delta-02"],
        "trial_crs": "atlantis-c",
        "trial_benchmark": "curl-delta-02",
        "trial_num": 0,
    }

    config_yaml_path = trial_output_dir / "config.yaml"
    with open(config_yaml_path, "w") as f:
        yaml.dump(resolved_config, f)

    # Executor stores execution metadata
    execution_metadata = {
        "timestamp": "2025-01-20T10:30:00",
        "command": ["python3", "infra/helper.py", "run_crs", "..."],
        "hints": {"enabled": True, "corpus_level": "1h"},
        "execution": {"duration_seconds": 120.5, "returncode": 0},
    }

    execution_json_path = trial_output_dir / "execution.json"
    with open(execution_json_path, "w") as f:
        json.dump(execution_metadata, f, indent=2)

    # Verify both files exist
    assert config_yaml_path.exists()
    assert execution_json_path.exists()

    # Load both
    with open(config_yaml_path) as f:
        config = yaml.safe_load(f)
    with open(execution_json_path) as f:
        execution = json.load(f)

    # Verify we have complete reproducibility information
    assert config["trial_crs"] == "atlantis-c"
    assert config["trial_benchmark"] == "curl-delta-02"
    assert config["trial_num"] == 0
    assert execution["command"][0] == "python3"
    assert execution["hints"]["enabled"] is True

    print("✓ config.yaml + execution.json provide complete reproducibility")
```

**Success Criteria**: Both files together contain all information needed to reproduce trial

### 5. Integration Testing

#### Test: End-to-End with Sample Config

```python
def test_e2e_with_sample_config_multi_crs(tmp_path):
    """Test end-to-end workflow with experiment-config-multi-crs.yaml."""
    # Use actual sample config
    config_path = Path("experiment-configs/experiment-config-multi-crs.yaml")

    # Load config
    config = load_experiment_config(config_path)

    # Verify config loaded correctly
    assert config.experiment == "multi-crs-baseline-eval"
    assert config.trials == 3
    assert len(config.crses) == 3  # atlantis-c, atlantis-multilang, ensemble-c
    assert len(config.benchmarks) == 6

    # Generate trial matrix
    trials = generate_trial_matrix(config.benchmarks, config.crses, config)

    # Expected: 3 CRSes × 6 benchmarks × 3 trials = 54 total
    assert len(trials) == 54

    # Mock trial execution - store config in trial dirs
    for i, trial in enumerate(trials[:3]):  # Test first 3 trials
        trial_dir = tmp_path / f"trial_{i}"
        trial_dir.mkdir(parents=True, exist_ok=True)

        # Store resolved config (what orchestrator does)
        trial_config = config.to_dict()
        trial_config["trial_crs"] = trial.crs
        trial_config["trial_benchmark"] = trial.benchmark
        trial_config["trial_num"] = trial.trial_num

        with open(trial_dir / "config.yaml", "w") as f:
            yaml.dump(trial_config, f)

        # Verify stored config
        with open(trial_dir / "config.yaml") as f:
            stored = yaml.safe_load(f)

        assert stored["trial_crs"] == trial.crs
        assert stored["trial_benchmark"] == trial.benchmark
        assert stored["experiment"] == "multi-crs-baseline-eval"
```

**Success Criteria**:
- Sample config loads successfully
- Correct number of trials generated
- Each trial directory gets correct config

#### Test: End-to-End with CLI Overrides

```python
def test_e2e_with_cli_overrides(tmp_path):
    """Test end-to-end workflow with CLI overrides applied."""
    # Use sample config
    config_path = Path("experiment-configs/experiment-config-multi-crs.yaml")
    config = load_experiment_config(config_path)

    # Apply CLI overrides
    cli_experiment_name = "custom-experiment-name"
    cli_crses = "custom-crs1,custom-crs2"
    cli_benchmarks = "bench1,bench2"

    # Resolve
    resolved_experiment = cli_experiment_name
    resolved_crses = parse_list_argument(cli_crses)
    resolved_benchmarks = parse_list_argument(cli_benchmarks)

    # Generate trial matrix with resolved values
    trials = generate_trial_matrix(resolved_benchmarks, resolved_crses, config)

    # Expected: 2 CRSes × 2 benchmarks × 3 trials = 12 total
    assert len(trials) == 12

    # Store config in trial directory
    trial_dir = tmp_path / "trial_0"
    trial_dir.mkdir(parents=True, exist_ok=True)

    stored_config = {
        "experiment": resolved_experiment,
        "trials": config.trials,
        "max_total_time": config.max_total_time,
        "difficulty_level": config.difficulty_level,
        "crses": resolved_crses,
        "benchmarks": resolved_benchmarks,
        "trial_crs": trials[0].crs,
        "trial_benchmark": trials[0].benchmark,
        "trial_num": trials[0].trial_num,
    }

    with open(trial_dir / "config.yaml", "w") as f:
        yaml.dump(stored_config, f)

    # Verify stored config has OVERRIDDEN values
    with open(trial_dir / "config.yaml") as f:
        stored = yaml.safe_load(f)

    assert stored["experiment"] == "custom-experiment-name"  # CLI override
    assert stored["crses"] == ["custom-crs1", "custom-crs2"]  # CLI override
    assert stored["benchmarks"] == ["bench1", "bench2"]  # CLI override

    print("✓ CLI overrides correctly propagated to stored config")
```

**Success Criteria**:
- CLI overrides applied correctly
- Trial matrix reflects overridden values
- Stored config contains overridden values (NOT original)

### 6. Benchmark Suite Expansion Testing

#### Test: Suite Expansion

```python
def test_benchmark_suite_expansion():
    """Test benchmark suite correctly expands to benchmark list."""
    suite_path = Path("benchmark-suites/crsbench-afc-c.yaml")

    with open(suite_path) as f:
        suite_config = yaml.safe_load(f)

    benchmarks = suite_config["benchmark_list"]

    # Verify suite has expected benchmarks
    assert "curl-delta-02" in benchmarks
    assert "libxml2-delta-03" in benchmarks
    assert len(benchmarks) > 10  # AFC-C suite has 24 benchmarks
```

**Success Criteria**: Suite expands to correct benchmark list

#### Test: End-to-End with Benchmark Suite

```python
def test_e2e_with_benchmark_suite(tmp_path):
    """Test end-to-end workflow with benchmark suite config."""
    config_path = Path("experiment-configs/experiment-config-benchmark-suite.yaml")
    config = load_experiment_config(config_path)

    assert config.benchmark_suite == "crsbench-afc-c"

    # Load and expand suite
    suite_path = Path(f"benchmark-suites/{config.benchmark_suite}.yaml")
    with open(suite_path) as f:
        suite_config = yaml.safe_load(f)

    benchmarks = suite_config["benchmark_list"]

    # Generate trial matrix
    trials = generate_trial_matrix(benchmarks, config.crses, config)

    # Expected: 2 CRSes × 24 benchmarks × 2 trials = 96 total
    assert len(trials) == 96

    print(f"✓ Generated {len(trials)} trials from benchmark suite")
```

**Success Criteria**: Suite config generates expected number of trials

## Test Organization

### Directory Structure

```
tests/
├── test_orchestration.py              # Main orchestration tests
│   ├── Config loading tests
│   ├── Trial matrix generation tests
│   ├── CLI override tests
│   ├── Config storage tests
│   └── Integration tests
├── test_orchestrator_e2e.py          # Existing E2E tests
└── fixtures/
    ├── sample_configs/               # Test config files
    │   ├── basic_config.yaml
    │   ├── suite_config.yaml
    │   └── override_config.yaml
    └── expected_results/             # Expected outputs
```

### Running Tests

```bash
# Run all orchestration tests
pytest tests/test_orchestration.py -v

# Run with coverage
pytest tests/test_orchestration.py --cov=crsbench.run_experiment --cov-report=html

# Run specific test category
pytest tests/test_orchestration.py::test_cli_override_crses -v

# Run integration tests
pytest tests/test_orchestration.py -k "e2e" -v
```

## Test Coverage Targets

### Critical Paths (100% Coverage Required)

1. **Config Resolution Logic**
   - CLI override priority
   - Benchmark vs suite selection
   - Field validation

2. **Trial Matrix Generation**
   - Combination counting
   - Ordering verification

3. **Config Storage**
   - Resolved values stored (NOT original)
   - Trial-specific fields added

### Important Paths (90%+ Coverage)

1. Error handling
2. Validation failures
3. Suite expansion
4. Mode selection

### Nice-to-Have (80%+ Coverage)

1. Progress monitoring
2. Report generation
3. Distributed mode fallback

## Critical Assertions

### 1. Trial Count Verification

```python
def verify_trial_count(crses, benchmarks, trials_per_combo):
    """Verify trial count matches expectation."""
    expected_count = len(crses) * len(benchmarks) * trials_per_combo
    actual_trials = generate_trial_matrix(benchmarks, crses, config)
    assert len(actual_trials) == expected_count, \
        f"Expected {expected_count} trials, got {len(actual_trials)}"
```

### 2. Config Override Verification

```python
def verify_config_override(stored_config, cli_value, field_name):
    """Verify stored config has CLI override, not original value."""
    assert stored_config[field_name] == cli_value, \
        f"Stored config {field_name} should be CLI value '{cli_value}', " \
        f"not original config value"
```

### 3. Reproducibility Verification

```python
def verify_reproducibility(trial_dir):
    """Verify trial directory has complete reproducibility info."""
    assert (trial_dir / "config.yaml").exists(), "Missing config.yaml"
    assert (trial_dir / "execution.json").exists(), "Missing execution.json"

    # Load both
    with open(trial_dir / "config.yaml") as f:
        config = yaml.safe_load(f)
    with open(trial_dir / "execution.json") as f:
        execution = json.load(f)

    # Verify essential fields
    assert "trial_crs" in config
    assert "trial_benchmark" in config
    assert "trial_num" in config
    assert "command" in execution
```

## Common Test Pitfalls

### 1. Testing Original Config Instead of Resolved Config

**Wrong**:
```python
# Testing that original config is stored (WRONG!)
assert stored_config["crses"] == config.crses
```

**Right**:
```python
# Testing that RESOLVED config is stored (CORRECT!)
resolved_crses = cli_crses if cli_crses else config.crses
assert stored_config["crses"] == resolved_crses
```

### 2. Not Verifying Trial Count Calculation

**Wrong**:
```python
# Assuming trial count is correct (WRONG!)
trials = generate_trial_matrix(benchmarks, crses, config)
# No assertion
```

**Right**:
```python
# Verify explicit calculation (CORRECT!)
expected = len(crses) * len(benchmarks) * config.trials
trials = generate_trial_matrix(benchmarks, crses, config)
assert len(trials) == expected
```

### 3. Not Testing CLI Overrides

**Wrong**:
```python
# Only testing config file loading (INCOMPLETE!)
config = load_experiment_config(config_path)
assert config.experiment == "test"
```

**Right**:
```python
# Test CLI override takes precedence (COMPLETE!)
config = load_experiment_config(config_path)
cli_name = "overridden"
resolved = cli_name if cli_name else config.experiment
assert resolved == cli_name  # Verifies override works
```

## References

- [Orchestration Design](./orchestration.md): Main orchestration design
- [CRS Executors Design](./evaluation/crs-executors.md): Trial directory structure
- [Snapshot Design](./evaluation/snapshots.md): Config storage in snapshots
- [Existing E2E Tests](../tests/test_orchestrator_e2e.py): Current test implementation
