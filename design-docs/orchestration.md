# Experiment Orchestration Design

This document describes the implementation of `crsbench/run_experiment.py`, the main CLI entry point and orchestration layer for running CRS evaluations.

## Purpose

The orchestration module provides:
- Single CLI entry point (`crsbench` command) for running experiments
- Flexible configuration with CLI overrides
- Automatic selection of local vs. distributed execution modes
- Trial matrix generation and job scheduling
- Progress monitoring and result aggregation

## Architecture Overview

### Command Flow

```
User runs: crsbench --experiment-config config.yaml

    ↓
1. Parse CLI Arguments
    ├── --experiment-config (required)
    ├── --experiment-name (optional override)
    ├── --crses (optional override)
    ├── --benchmarks (optional override)
    ├── --benchmark-suite (optional override)
    └── --local-only (optional flag)
    ↓
2. Load & Validate Config
    └── Load experiment-config.yaml
    └── Validate with ExperimentConfig schema
    └── Exit if invalid
    ↓
3. Resolve Parameters (CLI overrides config)
    ├── experiment_name: CLI --experiment-name > config.experiment
    ├── crses: CLI --crses > config.crses
    └── benchmarks: CLI --benchmarks > CLI --benchmark-suite >
                    config.benchmarks > config.benchmark_suite
    ↓
4. Generate Trial Matrix
    └── trials = crses × benchmarks × config.trials
    └── Each trial = (crs, benchmark, trial_num)
    ↓
5. Determine Execution Mode
    ├── Local mode if:
    │   ├── --local-only flag set
    │   ├── Only 1 total trial
    │   ├── No redis_host configured
    │   └── Redis not available
    └── Distributed mode otherwise
    ↓
6. Execute Trials
    ├── Local Mode: Sequential execution in current process
    └── Distributed Mode: Enqueue to Redis/RQ, monitor progress
    ↓
7. Generate Report
    └── Aggregate results, calculate statistics, display summary
```

## CLI Interface

### Installation

```bash
# Install in editable mode
uv pip install -e .

# Creates executable: .venv/bin/crsbench
```

### Command Signature

```bash
crsbench \
  --experiment-config CONFIG_FILE \
  [--experiment-name EXPERIMENT_NAME] \
  [--crses CRS_LIST] \
  [--benchmarks BENCHMARK_LIST] \
  [--benchmark-suite SUITE_NAME] \
  [--local-only]
```

**Key Design Decision**: All arguments except `--experiment-config` are optional, allowing the config file to be the source of truth with CLI providing overrides.

### Arguments

| Argument | Required | Type | Description |
|----------|----------|------|-------------|
| `--experiment-config` | ✅ Yes | Path | Path to experiment YAML config |
| `--experiment-name` | ❌ No | String | Override experiment identifier |
| `--crses` | ❌ No | CSV List | Override CRS list |
| `--benchmarks` | ❌ No | CSV List | Override benchmark list |
| `--benchmark-suite` | ❌ No | String | Override with suite name |
| `--local-only` | ❌ No | Flag | Force local execution |

**Mutual Exclusivity**: Cannot specify both `--benchmarks` and `--benchmark-suite`

### Usage Examples

**Minimal usage (all from config):**
```bash
crsbench --experiment-config my-experiment.yaml
```

**Override experiment name:**
```bash
crsbench --experiment-config config.yaml \
         --experiment-name test-run-v2
```

**Override CRS list:**
```bash
crsbench --experiment-config config.yaml \
         --crses custom-crs1,custom-crs2
```

**Override benchmarks with direct list:**
```bash
crsbench --experiment-config config.yaml \
         --benchmarks bench1,bench2,bench3
```

**Override benchmarks with suite:**
```bash
crsbench --experiment-config config.yaml \
         --benchmark-suite crsbench-afc-jvm
```

**Force local execution:**
```bash
crsbench --experiment-config config.yaml \
         --local-only
```

## Configuration Resolution

### Override Priority

The orchestration layer implements a clear priority system:

**For `experiment_name`:**
1. CLI `--experiment-name` (highest priority)
2. Config `experiment` field

**For `crses`:**
1. CLI `--crses` (highest priority)
2. Config `crses` field

**For `benchmarks`:**
1. CLI `--benchmarks` (highest priority)
2. CLI `--benchmark-suite`
3. Config `benchmarks` field
4. Config `benchmark_suite` field

### Resolution Logic

```python
def main():
    # Parse CLI args
    args = parse_arguments()

    # Load config
    config = load_experiment_config(args.experiment_config)

    # Resolve experiment name
    experiment_name = args.experiment_name if args.experiment_name else config.experiment

    # Resolve CRSes
    if args.crses:
        crses = parse_list_argument(args.crses)  # CLI override
    else:
        crses = config.crses  # From config

    # Resolve benchmarks
    if args.benchmarks and args.benchmark_suite:
        # Error: mutually exclusive
        sys.exit(1)
    elif args.benchmarks:
        benchmarks = parse_list_argument(args.benchmarks)  # CLI --benchmarks
    elif args.benchmark_suite:
        benchmarks = load_suite(args.benchmark_suite)  # CLI --benchmark-suite
    else:
        benchmarks = config.get_benchmark_list()  # From config
```

**Design Decision**: CLI arguments always override config values, never merge. This provides predictable behavior and avoids confusion.

### Logging Override Information

When an override is used, the orchestrator logs it clearly:

```
Experiment name: test-run-v2
  (overridden from CLI, config has: original-experiment)
CRSes (2): custom-crs1, custom-crs2
  (overridden from CLI, config has: atlantis-c, atlantis-multilang)
Benchmarks (3): bench1, bench2, bench3
  (overridden from CLI --benchmarks)
```

This transparency helps users understand exactly what configuration is being used.

## Trial Matrix Generation

### Trial Structure

```python
Trial = namedtuple('Trial', ['crs', 'benchmark', 'trial_num'])
```

Each trial represents a single execution unit:
- `crs`: CRS implementation to test
- `benchmark`: Benchmark project to test against
- `trial_num`: Trial number (0-indexed)

### Matrix Generation

```python
def generate_trial_matrix(benchmarks, crses, config):
    trials = []
    for crs in crses:
        for benchmark in benchmarks:
            for trial_num in range(config.trials):
                trials.append(Trial(crs, benchmark, trial_num))
    return trials
```

**Example**:
- CRSes: `[atlantis-c, atlantis-multilang]` (2)
- Benchmarks: `[bench1, bench2, bench3]` (3)
- Trials: `2` (from config)
- **Total**: 2 × 3 × 2 = **12 trials**

### Trial Ordering

Trials are ordered by:
1. CRS (outer loop)
2. Benchmark (middle loop)
3. Trial number (inner loop)

This ordering ensures that:
- All benchmarks for a CRS are tested together
- Multiple trials of same CRS+benchmark are grouped
- Results can be easily aggregated by CRS or benchmark

## Execution Modes

### Mode Selection Logic

```python
def should_use_distributed_mode(args, config, total_jobs):
    # User explicit override
    if args.local_only:
        return False

    # Single job is always local
    if total_jobs == 1:
        return False

    # No Redis configured
    if not config.redis_host or config.redis_host == "none":
        return False

    # Redis not available
    if not check_redis_available(config.redis_host):
        return False

    # Multiple jobs + Redis available = distributed
    return True
```

**Design Decision**: Default to distributed mode for multiple jobs when Redis is available, but provide easy local mode fallback.

### Local Mode

**When Used**:
- Single trial only
- `--local-only` flag set
- No Redis configured
- Redis connection fails

**Implementation**:
```python
def run_experiment_local(experiment_name, config, benchmarks, crses):
    trials = generate_trial_matrix(benchmarks, crses, config)

    results = []
    for trial in trials:
        result = run_crs_trial(
            crs=trial.crs,
            benchmark=trial.benchmark,
            trial_num=trial.trial_num,
            config=config.to_dict()
        )
        results.append(result)

    generate_final_report(results, experiment_name, config)
```

**Characteristics**:
- Sequential execution in single process
- Direct function calls (no queue)
- Immediate result availability
- Simple error handling
- No worker coordination needed

### Distributed Mode

**When Used**:
- Multiple trials
- Redis configured and available
- Not explicitly disabled

**Implementation**:
```python
def run_experiment_distributed(experiment_name, config, benchmarks, crses):
    queue = initialize_queue(config.redis_host, experiment_name)
    trials = generate_trial_matrix(benchmarks, crses, config)

    jobs = []
    for trial in trials:
        job = queue.enqueue(
            'crsbench.distributed.jobs.run_crs_trial',
            crs=trial.crs,
            benchmark=trial.benchmark,
            trial_num=trial.trial_num,
            config=config.to_dict(),
            job_timeout=config.max_total_time
        )
        jobs.append(job)

    results = monitor_jobs(queue, jobs, experiment_name)
    generate_final_report(results, experiment_name, config)
```

**Characteristics**:
- Parallel execution across workers
- Job queue coordination via Redis/RQ
- Async result collection
- Progress monitoring
- Worker failure handling

## Progress Monitoring

### Basic Monitoring (No Rich)

Simple text-based progress display:

```
============================================================
Experiment: my-experiment
============================================================
Queued:    5
Started:   3
Finished:  2
Failed:    0
============================================================

Progress: 2/10 jobs complete (2 success, 0 failed)
```

Refreshes every 3 seconds.

### Rich UI Monitoring

If `rich` library available, provides enhanced display:

```
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃              Experiment: my-experiment          ┃
┣━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃  Status  ┃                Count                ┃
┣━━━━━━━━━━╋━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃  Queued  ┃                   5                 ┃
┃  Started ┃                   3                 ┃
┃ Finished ┃                   2                 ┃
┃  Failed  ┃                   0                 ┃
┃  Total   ┃                  10                 ┃
┗━━━━━━━━━━┻━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

Refreshes every 1 second with live updates.

### Job Status Tracking

```python
def monitor_jobs(queue, job_list, experiment_name):
    while True:
        completed = 0
        failed = 0
        for job in job_list:
            job.refresh()  # Update from Redis
            if job.is_finished:
                completed += 1
            elif job.is_failed:
                failed += 1

        if completed + failed >= len(job_list):
            break

        time.sleep(1)

    # Collect results
    results = [job.result for job in job_list if job.result]
    return results
```

## Report Generation

### Report Structure

```
Final Report for Experiment: my-experiment
============================================================
Total trials: 12
Successful: 10 (83.3%)
Failed: 2 (16.7%)

POV Discovery:
  Total POVs found: 45/60
  Overall success rate: 75.0%

Failed trials (2):
  [1] atlantis-c on bench3 (trial 0): Timeout after 300s
  [2] custom-crs on bench1 (trial 1): CRS crashed

============================================================
Report generation complete
Experiment filestore: /tmp/experiment-data
Report filestore: /tmp/report-data
============================================================
```

### Statistics Aggregation

```python
def generate_final_report(results, experiment_name, config):
    total_trials = len(results)
    successful_trials = sum(1 for r in results if r.get('success'))
    failed_trials = total_trials - successful_trials

    # POV statistics
    total_povs_found = sum(r.get('povs_found', 0) for r in results if r.get('success'))
    total_povs_available = sum(r.get('total_povs', 0) for r in results if r.get('success'))

    if total_povs_available > 0:
        success_rate = total_povs_found / total_povs_available
        logger.info(f"Overall success rate: {success_rate:.1%}")
```

## Error Handling

### Configuration Errors

**Invalid config file:**
```python
result = validate_experiment_config(config_path)
if not result.is_valid:
    logger.error("Experiment configuration validation failed:")
    for error in result.errors:
        logger.error(f"  - {error.message}")
    sys.exit(1)
```

**Mutual exclusivity violation:**
```python
if args.benchmarks and args.benchmark_suite:
    logger.error("Cannot specify both --benchmarks and --benchmark-suite")
    sys.exit(1)
```

**Missing benchmark suite:**
```python
suite_path = Path("benchmark-suites") / f"{args.benchmark_suite}.yaml"
if not suite_path.exists():
    logger.error(f"Benchmark suite file not found: {suite_path}")
    sys.exit(1)
```

### Runtime Errors

**Redis connection failure:**
```python
try:
    queue = initialize_queue(config.redis_host, experiment_name)
except Exception as e:
    logger.error(f"Failed to initialize queue: {e}")
    logger.error("Falling back to local execution mode")
    run_experiment_local(experiment_name, config, benchmarks, crses)
```

**Trial execution failure:**
```python
result = run_crs_trial(...)
if not result.get('success'):
    logger.error(f"✗ Failed: {result.get('error', 'Unknown error')}")
    # Continue with next trial
```

## Integration Points

### With Validation Module

```python
from crsbench.validation import validate_experiment_config
from crsbench.validation.schemas import ExperimentConfig

# Load and validate config
result = validate_experiment_config(config_path)
if not result.is_valid:
    # Handle errors
    sys.exit(1)

# Create validated config object
config = ExperimentConfig(**yaml.safe_load(config_file))
```

### With Distributed Module

```python
from crsbench.distributed.queue import initialize_queue, check_redis_available
from crsbench.distributed.jobs import run_crs_trial

# Check Redis availability
if check_redis_available(config.redis_host):
    # Use distributed mode
    queue = initialize_queue(config.redis_host, experiment_name)
    job = queue.enqueue('crsbench.distributed.jobs.run_crs_trial', ...)
```

### With Evaluation Module

```python
from crsbench.distributed.jobs import run_crs_trial

# Execute trial (local or via worker)
result = run_crs_trial(
    crs=trial.crs,
    benchmark=trial.benchmark,
    trial_num=trial.trial_num,
    config=config.to_dict()
)
```

## Design Decisions

### Why Optional CLI Arguments?

**Problem**: Original design required `--experiment-name` and `--crses` on CLI, duplicating config.

**Solution**: Make all CLI args optional, use config as source of truth.

**Benefits**:
- Cleaner command line for standard runs
- Config file contains complete experiment definition
- CLI provides convenient overrides for testing
- Consistent with infrastructure-as-code practices

**Trade-off**: Slightly more complex resolution logic, but much better UX.

### Why Priority-Based Resolution?

**Problem**: Need clear rules for when CLI overrides config.

**Solution**: Simple priority system: CLI always wins.

**Benefits**:
- Predictable behavior
- No ambiguity
- Easy to understand and explain
- No merging complexity

**Alternative Considered**: Merge CLI and config values
**Rejected Because**: Unpredictable results, hard to reason about

### Why Automatic Mode Selection?

**Problem**: Users shouldn't need to understand distributed architecture.

**Solution**: Automatically choose local vs. distributed based on context.

**Benefits**:
- Single job naturally uses local mode
- Multiple jobs automatically parallelize
- Graceful fallback if Redis unavailable
- Override available for testing

**Trade-off**: Less explicit control, but much simpler UX.

### Why Trial Matrix Structure?

**Problem**: Need to track all CRS-benchmark-trial combinations.

**Solution**: Generate explicit trial list with namedtuple.

**Benefits**:
- Clear representation of work to be done
- Easy to serialize for distributed execution
- Simple progress tracking
- Enables future features (resume, partial execution)

## Performance Considerations

### Startup Time

- Config validation: <100ms
- Trial matrix generation: O(n × m × t) where n=CRS, m=benchmarks, t=trials
- Redis connection: <50ms
- **Total startup**: <500ms typical

### Memory Usage

- Trial matrix: ~100 bytes per trial
- Job queue: Minimal overhead in Redis
- Result collection: ~1KB per trial result
- **Total memory**: <10MB for 1000 trials

### Scalability

**Horizontal Scaling**:
- Add more Redis workers for distributed mode
- Each worker processes trials independently
- Linear speedup with workers (up to trial count)

**Vertical Scaling**:
- Local mode limited by single machine
- Distributed mode limited by Redis throughput
- Typically Redis handles 1000s of jobs/sec

## Future Enhancements

### Planned Features

1. **Resume Capability**: Resume interrupted experiments
2. **Partial Execution**: Run subset of trials (e.g., failed trials only)
3. **Live Streaming**: Real-time POV discovery notifications
4. **Dynamic Scheduling**: Prioritize fast/slow benchmarks
5. **Resource Limits**: CPU/memory limits per trial
6. **Timeout Strategies**: Per-CRS timeout multipliers

### Extension Points

- Custom progress monitors (webhooks, Slack, etc.)
- Alternative queue backends (Celery, etc.)
- Custom result aggregators
- Experiment templates

## Testing Strategy

### Unit Tests

Test individual functions:
```python
def test_parse_list_argument():
    assert parse_list_argument("a,b,c") == ["a", "b", "c"]
    assert parse_list_argument(" a , b , c ") == ["a", "b", "c"]

def test_generate_trial_matrix():
    config = Mock(trials=2)
    trials = generate_trial_matrix(["b1", "b2"], ["c1"], config)
    assert len(trials) == 4  # 2 benchmarks × 1 CRS × 2 trials
```

### Integration Tests

Test full workflow:
```python
def test_run_experiment_local_mode(tmp_path):
    # Create config
    config_path = tmp_path / "config.yaml"
    config_path.write_text("""
    experiment: test
    trials: 1
    crses: [test-crs]
    benchmarks: [test-bench]
    ...
    """)

    # Run experiment
    result = subprocess.run([
        "crsbench",
        "--experiment-config", str(config_path),
        "--local-only"
    ])

    assert result.returncode == 0
```

### Test Coverage

- Argument parsing: 100%
- Config resolution: 100%
- Mode selection: 100%
- Error handling: 90%+
- Integration: Key workflows

## Common Pitfalls

### 1. Forgetting Config File

**Wrong**:
```bash
crsbench --experiment-name test
```

**Right**:
```bash
crsbench --experiment-config config.yaml
```

### 2. Specifying Both Benchmark Methods

**Wrong**:
```bash
crsbench --experiment-config config.yaml \
         --benchmarks bench1,bench2 \
         --benchmark-suite crsbench-afc-c  # Error!
```

**Right**:
```bash
# Choose one
crsbench --experiment-config config.yaml --benchmarks bench1,bench2
# OR
crsbench --experiment-config config.yaml --benchmark-suite crsbench-afc-c
```

### 3. Assuming Distributed Mode

**Wrong**:
```python
# Assuming distributed mode is always used
queue = initialize_queue(config.redis_host, ...)
```

**Right**:
```python
# Check and handle both modes
if should_use_distributed_mode(args, config, total_jobs):
    run_experiment_distributed(...)
else:
    run_experiment_local(...)
```

## References

- [Validation Module](./validation/validation.md): Config validation design
- [Distributed Module](./distributed/distributed-job-queue.md): Job queue design
- [Evaluation Module](./evaluation/evaluation.md): Trial execution design
- [Architecture](./architecture.md): Overall system architecture
