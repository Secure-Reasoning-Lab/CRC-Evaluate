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
User runs: crsbench run --experiment-config config.yaml

    ↓
1. Parse CLI Arguments
    ├── --experiment-config (required)
    ├── --local-only (optional flag)
    ├── --distributed (optional flag)
    └── --dry-run, --verbose
    ↓
2. Load & Validate Config
    └── Load experiment-config.yaml
    └── Validate with ExperimentConfig schema
    └── Exit if invalid
    ↓
3. Resolve Parameters (all from config)
    ├── experiment_name: config.experiment
    ├── crs_compose services: config.crs_compose
    └── benchmarks: config.benchmarks or config.benchmark_suite
    ↓
4. Generate Trial Matrix
    └── trials expand from CRS services × benchmark-harness × sanitizer × mode × trial_num
    └── Bug-fixing CRS adds CPV-targeted fan-out in delta/full modes
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
# Install dependencies
uv sync

# Creates executable: .venv/bin/crsbench
```

### Command Signature

```bash
crsbench run \
  --experiment-config CONFIG_FILE \
  [--local-only] \
  [--distributed] \
  [--queue-mode {fresh,continue,quit}] \
  [--retry-failed] \
  [--dry-run] \
  [--verbose]
```

**Key Design Decision**: The experiment config YAML is the source of truth for benchmarks, CRSes, mode, paths, and other experiment settings. The CLI provides only execution-control flags.

### Arguments

| Argument | Required | Type | Description |
|----------|----------|------|-------------|
| `--experiment-config` | Yes | Path | Path to experiment YAML config |
| `--local-only` | No | Flag | Force local execution |
| `--distributed` | No | Flag | Force distributed execution |
| `--queue-mode` | No | Choice | Existing queue policy (`fresh`, `continue`, `quit`) |
| `--retry-failed` | No | Flag | Requeue failed trials in continue mode |
| `--dry-run` | No | Flag | Show what would run without executing |
| `--verbose` / `-v` | No | Flag | Enable verbose output |

Benchmarks, CRSes, benchmark suites, mode, paths, hint settings, and all other experiment parameters are specified in the experiment config YAML.

### Usage Examples

**Minimal usage (benchmarks, `crs_compose`, etc. configured in YAML):**
```bash
crsbench run --experiment-config my-experiment.yaml
```

**Force local execution:**
```bash
crsbench run --experiment-config config.yaml \
             --local-only
```

**Dry run (show what would execute):**
```bash
crsbench run --experiment-config config.yaml \
             --dry-run
```

## Configuration Resolution

### Resolution Priority

The orchestration layer reads experiment parameters from the config YAML.
`experiment_name`, `crs_compose`, `benchmarks`, `benchmark_suite`, and all other
experiment settings come from config.

### Resolution Logic

```python
def main():
    # Parse CLI args
    args = parse_arguments()

    # Load config
    config = load_experiment_config(args.experiment_config)

    # Resolve experiment name from config
    experiment_name = config.experiment

    # CRSes from config
    crses = list(config.crs_compose.keys())

    # Benchmarks from config (benchmarks list or benchmark_suite)
    benchmarks = config.get_benchmark_list()
```

**Design Decision**: Benchmarks, CRSes, and other experiment parameters are config-only. This keeps the CLI simple and makes experiment configs self-contained and reproducible.

## Trial Matrix Generation

### Trial Structure

Each trial is a concrete execution unit that includes CRS, benchmark harness,
sanitizer, mode, and trial number, plus optional CPV targeting for bug-fixing CRS.

### Matrix Generation

Matrix construction is schema-driven and expands by benchmark-harness pairs,
mode/sanitizer combinations, and CPV targeting (for bug-fixing CRS).

**Note**:
- A simple product estimate (`CRS × benchmark × trials`) is only a lower bound.
- Actual job counts depend on harness count, sanitizers, mode, and CPV expansion.

### Trial Ordering

Trials are ordered by:
1. CRS (outer loop)
2. Benchmark harness
3. Mode
4. Sanitizer
5. CPV target (when applicable)
6. Trial number

This ordering ensures that:
- All harness variants for a CRS are tested together
- Multiple trials of same CRS+harness+mode+sanitizer(+CPV) are grouped
- Results can be easily aggregated by CRS or benchmark

## Execution Modes

### Mode Selection Logic

```python
def should_use_distributed_mode(args, config, total_jobs):
    if args.local_only and args.distributed:
        raise ValueError("--local-only and --distributed are mutually exclusive")

    # User explicit distributed override
    if args.distributed:
        redis_host = normalize_redis_host(config.redis_host)
        if redis_host is None:
            raise RuntimeError("--distributed requires redis_host in config")
        if not check_redis_available(redis_host):
            raise RuntimeError("--distributed requested but Redis is unavailable")
        return True

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

**Implementation (current shape)**:
```python
def run_experiment_local(experiment_name, config, trials):
    # trials already include harness/mode/sanitizer/(optional CPV) expansion
    trial_suffix = "_" + "".join(...)

    results = []
    for trial in trials:
        bh = trial.benchmark_harness
        trial_id = build_trial_id(experiment_name, trial, trial_suffix)
        result = run_crs_trial(
            crs=trial.crs,
            benchmark=bh.name,
            harness_name=bh.harness.name,
            harness_path=bh.harness.path,
            mode=trial.mode,
            sanitizer=trial.sanitizer,
            trial_num=trial.trial_num,
            trial_id=trial_id,
            config_dict=config.model_dump(),
            target_cpv_id=trial.target_cpv_id,
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
- Any expanded trial count other than exactly 1
- Redis configured and available
- Not explicitly disabled

**Implementation (current shape)**:
```python
def run_experiment_distributed(experiment_name, config, trials):
    session = DistributedRuntimeSession.for_run(
        redis_host=normalize_redis_host(config.redis_host),
        experiment_name=experiment_name,
    )
    queue = session.trial_queue

    existing = get_existing_trials(queue, experiment_name=experiment_name)
    queue_mode = resolve_queue_mode(existing, requested_mode, retry_failed)
    trials = filter_existing_trials(trials, existing, queue_mode)
    trials = filter_trials_already_complete_on_disk(trials, config)

    session.register_or_raise(RuntimeRegistration.from_experiment_config(config))
    trial_suffix = "_" + "".join(...)

    jobs = []
    for trial in trials:
        bh = trial.benchmark_harness
        trial_id = build_trial_id(experiment_name, trial, trial_suffix)
        job = queue.enqueue(
            'crsbench.distributed.jobs.run_crs_trial',
            crs=trial.crs,
            benchmark=bh.name,
            harness_name=bh.harness.name,
            mode=trial.mode,
            sanitizer=trial.sanitizer,
            target_cpv_id=trial.target_cpv_id,
            trial_num=trial.trial_num,
            trial_id=trial_id,
            harness_path=bh.harness.path,
            config_dict=config.model_dump(),
            job_timeout=config.max_total_time
        )
        jobs.append(job)

    results = monitor_jobs(queue, jobs, experiment_name, config)
    session.cleanup()
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
    successful_trials = sum(1 for r in results if r.success)
    failed_trials = total_trials - successful_trials

    # POV statistics
    total_povs_found = sum(r.povs_found for r in results if r.success)
    total_povs_available = sum(r.total_povs for r in results if r.success)

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

**Mutual exclusivity in config:**
```python
# Validated by ExperimentConfig schema
# Cannot specify both benchmarks and benchmark_suite in YAML
```

**Missing benchmark suite:**
```python
suite_path = Path("benchmark-suites") / f"{config.benchmark_suite}.yaml"
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
    raise RuntimeError("Distributed mode requires a reachable Redis backend") from e
```

**Trial execution failure:**
```python
result = run_crs_trial(...)
if not result.success:
    logger.error(f"✗ Failed: {result.error or 'Unknown error'}")
    raise RuntimeError(f"Trial failed: {result.error or 'Unknown error'}")
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
    benchmark=trial.benchmark_harness.name,
    harness_name=trial.benchmark_harness.harness.name,
    harness_path=trial.benchmark_harness.harness.path,
    trial_num=trial.trial_num,
    trial_id=build_trial_id(config.experiment, trial, "_sample"),
    config_dict=config.model_dump(),
    mode=trial.mode,
    sanitizer=trial.sanitizer,
    target_cpv_id=trial.target_cpv_id,
)
```

## Design Decisions

### Why Config-Only for Experiment Parameters?

**Problem**: Having 23+ CLI flags duplicated settings from the config YAML, creating confusion about which source of truth was active.

**Solution**: Remove CLI overrides for experiment parameters (benchmarks, CRSes, mode, paths, hints, etc.). The config YAML is the single source of truth. Only execution-control flags remain on the CLI.

**Benefits**:
- Simple, predictable CLI with only execution-control flags
- Config file is self-contained and reproducible
- No ambiguity about which values are active
- Consistent with infrastructure-as-code practices

**Trade-off**: Cannot do quick ad-hoc overrides from CLI, but this was rarely needed and caused confusion.

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

**Problem**: Need to track all concrete execution units after harness/mode/sanitizer/CPV expansion.

**Solution**: Generate explicit `Trial` objects from registry IDs and benchmark harness metadata.

**Benefits**:
- Clear representation of work to be done
- Easy to serialize for distributed execution
- Simple progress tracking
- Enables future features (resume, partial execution)

## Performance Considerations

### Startup Time

- Config validation: <100ms
- Trial matrix generation scales with expanded dimensions (CRS × harness × mode × sanitizer × trial, plus CPV fan-out for bug-fixing CRS)
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
    crs_compose:
      test-crs:
        num_cores: 8
    benchmarks: [test-bench]
    ...
    """)

# Run experiment
result = subprocess.run([
    "crsbench",
    "run",
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
crsbench run
```

**Right**:
```bash
crsbench run --experiment-config config.yaml
```

### 2. Trying to Override Benchmarks/CRSes from CLI

**Wrong** (these flags no longer exist on `crsbench run`):
```bash
crsbench run --experiment-config config.yaml \
             --benchmarks bench1 bench2 \
             --crses crs1,crs2
```

**Right** (specify in config YAML):
```yaml
# config.yaml
benchmarks:
  - bench1
  - bench2
crs_compose:
  crs1:
    num_cores: 8
  crs2:
    num_cores: 8
```
```bash
crsbench run --experiment-config config.yaml
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
