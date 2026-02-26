# Design Document: Distributed Job Queue System for CRSBench

**Author**: CRSBench Development Team
**Date**: 2025-10-13
**Status**: Implementation
**Reference**: Adapted from FuzzBench's Redis+RQ architecture

> **Note on Valkey**: CRSBench uses Valkey as the queue backend instead of Redis. Valkey is a fully Redis-compatible open-source data store. Throughout this document, "Redis" refers to the Redis protocol/interface that Valkey implements. The Python `redis` package and RQ (Redis Queue) library work seamlessly with Valkey. For deployment, see `services/valkey/docker-compose.yml`.

## 1. Overview

This document describes the design and implementation of a distributed job queue system for CRSBench, enabling scalable parallel execution of CRS (Cyber Reasoning System) trials across multiple worker processes.

### 1.1 Goals

- Enable horizontal scaling of CRS trial execution
- Support both local (docker-compose) and cloud deployments
- Provide reliable job tracking and failure recovery
- Real-time progress monitoring of experiments
- Minimize infrastructure complexity while maximizing flexibility

### 1.2 Non-Goals

- Auto-scaling based on queue depth (future enhancement)
- Advanced job prioritization (future enhancement)
- Multi-tenant job isolation (not needed for current use case)

## 2. Architecture Overview

### 2.1 High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                     CRSBench Experiment                          │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
         ┌────────────────────────────────────────┐
         │   Orchestrator (run_experiment.py)    │
         │                                        │
         │  • Parse CLI arguments                 │
         │  • Validate config                     │
         │  • Connect to Redis                    │
         │  • Generate trial matrix               │
         │  • Enqueue jobs                        │
         │  • Monitor progress                    │
         │  • Generate reports                    │
         └────────┬───────────────────────────────┘
                  │
                  │ RQ Job Enqueue
                  ▼
           ┌──────────────┐
           │ Redis Server │  (queue-server container)
           │              │
           │ • Job queue  │
           │ • Job status │
           │ • Results    │
           └──────┬───────┘
                  │
                  │ RQ Job Dequeue
                  ▼
    ┌─────────────────────────────────────────┐
    │     Workers (worker.py × N)             │
    │                                         │
    │  • Connect to Redis                     │
    │  • Poll for jobs                        │
    │  • Execute jobs                         │
    │  • Report results                       │
    │  • Repeat until queue empty             │
    └─────────────────────────────────────────┘
```

### 2.2 Technology Stack

- **Valkey 8.0+**: Message broker and job queue storage (Redis-compatible)
- **RQ (Redis Queue) 1.11.1+**: Python job queue library (works with Valkey)
- **Docker Compose**: Local orchestration
- **Python 3.11+**: Implementation language

> Note: The Python `redis` package is used to connect to Valkey, as Valkey is fully Redis-protocol-compatible.

## 3. Component Design

### 3.1 Orchestrator (run_experiment.py)

**Location**: `crsbench/run_experiment.py`

**Responsibilities**:
1. Parse command-line arguments
2. Validate experiment configuration
3. Initialize Redis queue connection
4. Generate trial execution matrix
5. Enqueue CRS trial jobs
6. Monitor job status continuously
7. Handle job failures
8. Generate final experiment reports

**Key Functions**:

```python
def main() -> None:
    """Main entry point - CLI handler."""
    args = parse_arguments()
    validate_arguments(args)

    # NEW: Redis-based orchestration
    config = load_experiment_config(args.experiment_config)
    redis_connection = redis.Redis(host=config.redis_host)

    with rq.Connection(redis_connection):
        run_experiment_with_queue(args, config)

def run_experiment_with_queue(args, config) -> None:
    """Run experiment using job queue."""
    trial_q, _build_q, _verify_q = resolve_queue_names(args.experiment_name)
    queue = rq.Queue(trial_q)

    # Generate trial matrix
    trials = generate_trial_matrix(args, config)

    # Enqueue jobs
    job_list = []
    for trial in trials:
        job = queue.enqueue(
            'crsbench.distributed.jobs.run_crs_trial',
            crs=trial.crs,
            benchmark=trial.benchmark,
            trial_num=trial.trial_num,
            config=config.to_dict(),
            job_timeout=config.max_total_time,
            result_ttl=-1
        )
        job_list.append(job)

    # Monitor progress
    monitor_jobs(queue, job_list, args.experiment_name)
```

**Integration Points**:
- Uses existing validation module: `crsbench.validation`
- Loads config using YAML parsing
- Interfaces with Redis/RQ for job management

### 3.2 Worker (worker.py)

**Location**: `crsbench/distributed/worker.py`

**Responsibilities**:
1. Connect to Redis queue server
2. Poll queue for available jobs
3. Execute jobs using existing evaluation infrastructure
4. Report results back to queue
5. Handle job failures gracefully
6. Terminate when queue is empty (burst mode)

**Implementation**:

```python
"""CRSBench worker for distributed job execution."""

import os
import time
import redis
import rq
import logging

logger = logging.getLogger(__name__)


def main():
    """Worker entry point - connects to Redis and processes jobs."""
    redis_host = os.environ.get('CRSBENCH_REDIS_HOST', 'localhost')
    experiment_name = 'default'  # Normally provided by config/CLI

    logger.info(f"Connecting to Redis at {redis_host}")
    redis_connection = redis.Redis(host=redis_host)

    with rq.Connection(redis_connection):
        queue_name, _build_q, _verify_q = resolve_queue_names(experiment_name)
        queue = rq.Queue(queue_name)
        worker = rq.Worker([queue])

        logger.info(f"Worker started, listening on queue: {queue_name}")

        # Work in burst mode until queue is empty
        while queue.count + queue.deferred_job_registry.count > 0:
            worker.work(burst=True)
            time.sleep(5)

        logger.info("Queue empty, worker shutting down")


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    main()
```

**Environment Variables**:
- `CRSBENCH_REDIS_HOST`: Redis server hostname (default: localhost)

### 3.3 Jobs Module (jobs.py)

**Location**: `crsbench/distributed/jobs.py`

**Responsibilities**:
Define all job types that can be executed by workers.

**Job Types**:

1. **build_crs_environment**: Prepare CRS execution environment
2. **run_crs_trial**: Execute a single CRS trial
3. **evaluate_crs_trial**: Evaluate and aggregate trial results

**Implementation**:

```python
"""Job definitions for CRSBench distributed execution."""

import logging
from pathlib import Path
from typing import Dict, Any
from crsbench.evaluation.runner import BenchmarkRunner
from crsbench.evaluation.adapter import create_adapter

logger = logging.getLogger(__name__)


def build_crs_environment(crs: str, benchmark: str, config: Dict[str, Any]) -> bool:
    """
    Prepare CRS execution environment.

    Args:
        crs: CRS implementation name
        benchmark: Benchmark identifier
        config: Experiment configuration

    Returns:
        bool: True if environment setup successful
    """
    logger.info(f"Building environment for {crs} on {benchmark}")

    # TODO: Implement CRS environment setup
    # - Validate CRS installation
    # - Prepare Docker images if needed
    # - Set up CRS-specific configuration

    return True


def run_crs_trial(
    crs: str,
    benchmark: str,
    trial_num: int,
    config: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Execute a single CRS trial.

    Args:
        crs: CRS implementation name
        benchmark: Benchmark identifier or path
        trial_num: Trial number for this execution
        config: Experiment configuration dictionary

    Returns:
        Dict containing trial results and metadata
    """
    logger.info(f"Running trial {trial_num} for {crs} on {benchmark}")

    try:
        # Initialize benchmark runner with CRS adapter
        adapter = create_adapter(config, crs, ...)
        runner = BenchmarkRunner(adapter=adapter)

        # Resolve benchmark path
        benchmark_path = resolve_benchmark_path(benchmark, config)

        # Run benchmark evaluation
        result = runner.run_benchmark(
            benchmark_path=benchmark_path,
            mode='auto',
            crs_config={'simulation_delay': 0.1, 'success_rate': 0.7}
        )

        # Prepare result dictionary
        trial_result = {
            'crs': crs,
            'benchmark': benchmark,
            'trial_num': trial_num,
            'success': result.is_valid,
            'povs_found': result.povs_found,
            'total_povs': result.total_povs,
            'success_rate': result.success_rate,
            'report': result.report.to_dict(),
            'metadata': {
                'experiment_filestore': config.get('experiment_filestore'),
                'timestamp': time.time()
            }
        }

        logger.info(f"Trial {trial_num} completed: {result.povs_found}/{result.total_povs} POVs found")

        return trial_result

    except Exception as e:
        logger.error(f"Trial {trial_num} failed: {str(e)}")
        return {
            'crs': crs,
            'benchmark': benchmark,
            'trial_num': trial_num,
            'success': False,
            'error': str(e),
            'metadata': {
                'timestamp': time.time()
            }
        }


def evaluate_crs_trial(trial_id: str, trial_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluate and aggregate CRS trial results.

    Args:
        trial_id: Unique trial identifier
        trial_data: Trial execution data

    Returns:
        Dict containing evaluation results
    """
    logger.info(f"Evaluating trial {trial_id}")

    # TODO: Implement evaluation logic
    # - POV deduplication
    # - Patch validation
    # - Result aggregation

    return {
        'trial_id': trial_id,
        'evaluation_complete': True
    }


def resolve_benchmark_path(benchmark: str, config: Dict[str, Any]) -> Path:
    """
    Resolve benchmark identifier to filesystem path.

    Args:
        benchmark: Benchmark identifier or path
        config: Experiment configuration

    Returns:
        Path to benchmark directory
    """
    # If it's already a path, use it
    if Path(benchmark).exists():
        return Path(benchmark)

    # Otherwise, look in standard locations
    benchmarks_root = Path('/Users/fuyu0425/aixcc/CRSBench/benchmarks')
    benchmark_path = benchmarks_root / benchmark

    if benchmark_path.exists():
        return benchmark_path

    raise FileNotFoundError(f"Benchmark not found: {benchmark}")
```

### 3.4 Queue Utilities Module

**Location**: `crsbench/distributed/queue.py`

**Decision**: Create separate module within `crsbench/distributed/` for better organization

```python
"""Utilities for Redis queue management."""

import redis
import rq
from typing import List


def initialize_queue(redis_host: str, experiment_name: str) -> rq.Queue:
    """
    Initialize Redis-backed RQ queue.

    Args:
        redis_host: Redis server hostname
        experiment_name: Experiment identifier for queue naming

    Returns:
        Initialized RQ queue
    """
    queue_name, _build_q, _verify_q = resolve_queue_names(experiment_name)
    redis_connection = redis.Redis(host=redis_host)
    queue = rq.Queue(queue_name, connection=redis_connection)
    return queue


def get_all_jobs(queue: rq.Queue) -> List[rq.job.Job]:
    """
    Get all jobs in queue.

    Args:
        queue: RQ queue instance

    Returns:
        List of Job objects
    """
    job_ids = queue.get_job_ids()
    return rq.job.Job.fetch_many(job_ids, queue.connection)
```

## 4. Data Flow

### 4.1 Job Lifecycle

```
1. Job Creation (Orchestrator)
   └─> queue.enqueue(run_crs_trial, ...)
       └─> Redis: Job stored with status='queued'

2. Job Assignment (Redis)
   └─> Worker polls queue
       └─> Redis: Job status='started'

3. Job Execution (Worker)
   └─> worker.work(burst=True)
       └─> Import and execute 'crsbench.distributed.jobs.run_crs_trial'
           └─> BenchmarkRunner.run_benchmark()
               └─> OssCrsAdapter.run()
                   └─> Generate results

4. Job Completion (Worker → Redis)
   └─> Job status='finished', result stored
       └─> Redis: Result TTL = -1 (persist forever)

5. Result Collection (Orchestrator)
   └─> Poll job.result for all jobs
       └─> Generate final experiment report
```

### 4.2 Trial Matrix Generation

```python
def generate_trial_matrix(config):
    """
    Generate all trial combinations.

    For experiment with:
    - CRSes: [crs1, crs2]
    - Benchmarks: [bench1, bench2]
    - Trials: 3

    Generates 12 trials:
    (crs1, bench1, trial=0)
    (crs1, bench1, trial=1)
    (crs1, bench1, trial=2)
    (crs1, bench2, trial=0)
    ...
    (crs2, bench2, trial=2)
    """
    # CRSes and benchmarks are always from the experiment config YAML
    benchmarks = config.get_benchmark_list()
    crses = config.crses

    trials = []
    for crs in crses:
        for benchmark in benchmarks:
            for trial_num in range(config.trials):
                trials.append(Trial(crs, benchmark, trial_num))

    return trials
```

## 5. Configuration Schema Updates

### 5.1 Experiment Configuration

Add `redis_host` and `benchmarks_root` fields to experiment configuration:

**Configuration Fields**:

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| trials | int | Yes | - | Number of trials per CRS-benchmark combination (≥1) |
| max_total_time | int | Yes | - | Maximum time in seconds per trial (≥1) |
| difficulty_level | int | Yes | - | Difficulty level (0-4) controlling assistance provided |
| experiment_filestore | str | Yes | - | Directory for storing experiment data and results |
| report_filestore | str | Yes | - | Directory for HTML reports and summary data |
| redis_host | str | No | None | Redis server hostname/IP (omit or set to "none" for local mode) |
| benchmarks_root | str | No | ./benchmarks | Root directory containing benchmark projects |

**Benchmark Path Resolution**:

The `benchmarks_root` field specifies where to find benchmark directories. Path resolution follows this order:

1. **Absolute path**: If benchmark argument is an absolute path, use it directly
2. **Config root**: If `benchmarks_root` specified, look in `{benchmarks_root}/{benchmark_id}`
3. **Default root**: Look in `./benchmarks/{benchmark_id}` (relative to repo root)
4. **Error**: Raise `FileNotFoundError` if benchmark not found

**Usage Examples**:

```yaml
# Example 1: Standard configuration with default benchmarks location
# Benchmarks expected in ./benchmarks/
trials: 1
max_total_time: 86400
difficulty_level: 1
experiment_filestore: /tmp/crsbench/experiment-data
report_filestore: /tmp/crsbench/report-data
redis_host: queue-server

# Example 2: Custom benchmarks directory
# Useful for testing with benchmarks in non-standard location
trials: 1
max_total_time: 86400
difficulty_level: 1
experiment_filestore: /tmp/crsbench/experiment-data
report_filestore: /tmp/crsbench/report-data
redis_host: queue-server
benchmarks_root: /custom/path/to/benchmarks

# Example 3: Local mode without Redis (single job)
# benchmarks_root omitted, uses ./benchmarks/
trials: 1
max_total_time: 3600
difficulty_level: 0
experiment_filestore: /tmp/crsbench/experiment-data
report_filestore: /tmp/crsbench/report-data
# redis_host: none  # Omit for local mode

# Example 4: CI/CD configuration with absolute paths
trials: 1
max_total_time: 1800
difficulty_level: 1
experiment_filestore: /var/lib/crsbench/experiments
report_filestore: /var/lib/crsbench/reports
benchmarks_root: /opt/crsbench/benchmarks
```

**Benchmark Path Resolution**:

```python
# Benchmarks are specified in the experiment config YAML:
#   benchmarks:
#     - bench1
#     - bench2

# Resolution for benchmark "bench1":
# 1. Check if "bench1" is absolute path: No
# 2. Check config.benchmarks_root:
#    - If set: {benchmarks_root}/bench1
#    - If not set: ./benchmarks/bench1
# 3. If found: Use that path
# 4. If not found: Raise FileNotFoundError
```

### 5.2 Schema Validation Update

**File**: `crsbench/validation/schemas.py`

```python
class ExperimentConfig(BaseModel):
    """Experiment configuration schema."""

    trials: int = Field(..., ge=1)
    max_total_time: int = Field(..., ge=1)
    difficulty_level: int = Field(..., ge=0, le=4)
    experiment_filestore: str = Field(...)
    report_filestore: str = Field(...)
    redis_host: Optional[str] = Field(
        default=None,
        description="Redis server hostname (optional, omit for local mode)"
    )
    benchmarks_root: Optional[str] = Field(
        default=None,
        description="Root directory containing benchmarks (defaults to ./benchmarks)"
    )

    @validator('redis_host')
    def validate_redis_host(cls, v):
        if v and v.strip() and v.strip().lower() != 'none':
            return v.strip()
        return None  # Treat empty or "none" as None

    @validator('benchmarks_root')
    def validate_benchmarks_root(cls, v):
        if v and v.strip():
            path = Path(v.strip())
            if not path.exists():
                raise ValueError(f"Benchmarks root directory does not exist: {v}")
            if not path.is_dir():
                raise ValueError(f"Benchmarks root must be a directory: {v}")
            return str(path.absolute())
        return None  # Use default if not specified
```

## 6. Docker Infrastructure

### 6.1 Docker Compose Architecture

**File**: `services/valkey/docker-compose.yml` (Production-ready Valkey service)

See `services/valkey/docker-compose.yml` for the standalone Valkey service configuration.

**Full Stack Example**: `compose/crsbench.yaml` (Orchestrator + Workers + Valkey)

```yaml
version: "3.8"

services:
  queue-server:
    image: valkey/valkey:8.0-alpine
    # NOTE: Ports NOT exposed to host by default for security
    # Only accessible within Docker network
    expose:
      - "6379"
    healthcheck:
      test: ["CMD", "valkey-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5
    volumes:
      - valkey-data:/data
    command: valkey-server --appendonly yes

  run-experiment:
    build:
      context: ..
      dockerfile: docker/crsbench/Dockerfile
    depends_on:
      queue-server:
        condition: service_healthy
    environment:
      - CRSBENCH_REDIS_HOST=queue-server
    volumes:
      - experiment-data:/tmp/experiment-data
      - report-data:/tmp/report-data
    command: >
      crsbench run
      --experiment-config /config/experiment-config.yaml


  worker:
    build:
      context: ..
      dockerfile: docker/worker/Dockerfile
    depends_on:
      - run-experiment
      - queue-server
    environment:
      - CRSBENCH_REDIS_HOST=queue-server
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - experiment-data:/tmp/experiment-data
      - report-data:/tmp/report-data
    deploy:
      replicas: 2  # Start with 2 workers

volumes:
  valkey-data:
  experiment-data:
  report-data:
```

### 6.2 Worker Dockerfile

**File**: `docker/worker/Dockerfile`

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY pyproject.toml .
RUN pip install -e .

# Copy source code
COPY crsbench/ crsbench/

# Set Python path
ENV PYTHONPATH=/app

# Start worker
CMD ["python", "-m", "crsbench.distributed.worker"]
```

### 6.3 Worker Startup Script

**File**: `docker/worker/startup-worker.sh`

```bash
#!/bin/bash
# Worker startup script for RQ worker

set -e

CRSBENCH_REDIS_HOST=${CRSBENCH_REDIS_HOST:-localhost}

echo "Starting CRSBench worker"
echo "Redis host: $CRSBENCH_REDIS_HOST"
echo "Experiment: configured via config/CLI"

# Start RQ worker
python -m crsbench.distributed.worker
```

## 7. Integration with Existing Code

### 7.1 Leveraging Existing Modules

**Evaluation Module** (`crsbench/evaluation/`):
- Use `BenchmarkRunner` for trial execution
- Use `OssCrsAdapter` via `create_adapter()` for CRS execution
- Use `ResultCollector` and `EvaluationReport` for results

**Validation Module** (`crsbench/validation/`):
- Validate experiment configuration with updated schema
- Validate benchmark formats before enqueuing

**Deduplication Module** (`crsbench/deduplication/`):
- Use in `evaluate_crs_trial` job for POV deduplication

**Patch Tester Module** (`crsbench/patch_tester/`):
- Use in `evaluate_crs_trial` job for patch validation

### 7.2 Minimal Code Changes

The distributed job queue wraps existing functionality:

```python
# Before (single-threaded)
for crs in crses:
    for benchmark in benchmarks:
        result = runner.run_benchmark(benchmark, crs_config)

# After (distributed)
for crs in crses:
    for benchmark in benchmarks:
        job = queue.enqueue(run_crs_trial, crs, benchmark, config)
        jobs.append(job)

# Wait for completion
wait_for_jobs(jobs)
```

## 8. Job Status Monitoring

### 8.1 Status Tracking

RQ provides built-in job registries:

```python
def monitor_jobs(queue, job_list, experiment_name):
    """Monitor job progress and display status."""
    while True:
        print(f"\n{'='*60}")
        print(f"Experiment: {experiment_name}")
        print(f"{'='*60}")
        print(f"Queued:    {queue.count}")
        print(f"Started:   {queue.started_job_registry.count}")
        print(f"Deferred:  {queue.deferred_job_registry.count}")
        print(f"Finished:  {queue.finished_job_registry.count}")
        print(f"Failed:    {queue.failed_job_registry.count}")
        print(f"{'='*60}\n")

        # Check individual job status
        for job in job_list:
            status = job.get_status()
            print(f"  [{status:>8}] {job.id}")

        # Check if all jobs completed
        if all(job.result is not None or job.is_failed for job in job_list):
            break

        time.sleep(3)
```

### 8.2 Progress Visualization

Use Rich library for enhanced terminal output:

```python
from rich.console import Console
from rich.table import Table
from rich.live import Live

def monitor_jobs_rich(queue, job_list, experiment_name):
    """Monitor jobs with Rich UI."""
    console = Console()

    with Live(generate_status_table(queue, job_list), refresh_per_second=1) as live:
        while not all_jobs_complete(job_list):
            time.sleep(1)
            live.update(generate_status_table(queue, job_list))
```

## 9. Error Handling and Reliability

### 9.1 Job Failure Handling

```python
def run_crs_trial_with_retry(crs, benchmark, trial_num, config, max_retries=3):
    """Execute trial with retry logic."""
    for attempt in range(max_retries):
        try:
            return run_crs_trial(crs, benchmark, trial_num, config)
        except Exception as e:
            logger.warning(f"Trial attempt {attempt+1} failed: {e}")
            if attempt == max_retries - 1:
                logger.error(f"Trial failed after {max_retries} attempts")
                raise
            time.sleep(2 ** attempt)  # Exponential backoff
```

### 9.2 Worker Failure Recovery

- Jobs persist in Redis if worker crashes
- Failed jobs tracked in `failed_job_registry`
- Can manually retry failed jobs:

```python
def retry_failed_jobs(queue):
    """Retry all failed jobs."""
    failed_registry = queue.failed_job_registry
    for job_id in failed_registry.get_job_ids():
        job = rq.job.Job.fetch(job_id, connection=queue.connection)
        job.retry()
```

## 10. Testing Strategy

### 10.1 Unit Tests

**test_jobs.py**:
```python
def test_run_crs_trial_success():
    """Test successful trial execution."""
    result = run_crs_trial('test-crs', 'test-benchmark', 0, {})
    assert result['success'] is True
    assert 'povs_found' in result

def test_run_crs_trial_failure():
    """Test trial failure handling."""
    result = run_crs_trial('invalid-crs', 'invalid-benchmark', 0, {})
    assert result['success'] is False
    assert 'error' in result
```

**test_worker.py**:
```python
@patch('redis.Redis')
def test_worker_connects_to_redis(mock_redis):
    """Test worker Redis connection."""
    # Mock Redis connection
    # Test worker initialization
```

**test_queue_utils.py**:
```python
def test_initialize_queue():
    """Test queue initialization."""
    queue = initialize_queue('localhost', 'test-exp')
    assert queue.name == 'crsbench_test-exp'
```

### 10.2 Integration Tests

**test_job_queue_integration.py**:
```python
def test_job_enqueue_and_execute(redis_server):
    """Test job enqueue and worker execution."""
    # Start local Redis
    # Enqueue test job
    # Start worker
    # Verify job completion
```

### 10.3 End-to-End Tests

**test_distributed_execution.py**:
```python
def test_multi_worker_execution(docker_compose):
    """Test execution with multiple workers."""
    # Start compose with 2 workers
    # Enqueue 10 jobs
    # Verify all complete
    # Check results stored correctly
```

## 11. Performance Considerations

### 11.1 Job Granularity

- **Choice**: One job per trial (not per POV or per benchmark)
- **Rationale**: Balance between parallelism and overhead
- **Trade-off**: Finer granularity = more parallelism but more overhead

### 11.2 Worker Scaling

**Local Development**:
- 2-4 workers sufficient for testing
- Limited by available CPU cores

**Cloud Deployment**:
- Scale workers based on queue depth
- Monitor resource utilization
- Set maximum worker count based on budget

### 11.3 Redis Memory Usage

- Job results stored with `result_ttl=-1` (persist forever)
- Monitor Redis memory usage
- Periodically archive completed experiments
- Consider Redis persistence configuration

## 12. Future Enhancements

### 12.1 Auto-Scaling (Phase 2)

```python
def auto_scale_workers(queue, min_workers=1, max_workers=10):
    """Dynamically scale worker count based on queue depth."""
    queued_jobs = queue.count + queue.started_job_registry.count
    desired_workers = min(max(queued_jobs, min_workers), max_workers)

    # GCE: resize instance group
    # Docker: scale service
    # K8s: update deployment replicas
```

### 12.2 Job Priorities

```python
# High priority for small benchmarks
queue.enqueue(run_crs_trial, ..., priority='high')

# Low priority for large benchmarks
queue.enqueue(run_crs_trial, ..., priority='low')
```

### 12.3 Advanced Monitoring

- Grafana dashboards for queue metrics
- Prometheus exporter for RQ statistics
- Alert on job failures or queue backlog

### 12.4 Job Dependencies

```python
# Run evaluation only after trial completes
trial_job = queue.enqueue(run_crs_trial, ...)
eval_job = queue.enqueue(evaluate_crs_trial, depends_on=trial_job)
```

## 13. Migration Path

### 13.1 Local Mode (No Redis Required)

**Goal**: Allow users to run single-job experiments without Redis setup.

**When to Use Local Mode**:
- Development and testing with single CRS/benchmark
- Quick trial runs without distributed infrastructure
- CI/CD pipelines for validation
- Resource-constrained environments

**Implementation Strategy**:

```python
def main() -> None:
    """Main entry point with automatic mode detection."""
    args = parse_arguments()
    validate_arguments(args)

    config = load_experiment_config(args.experiment_config)

    # Determine execution mode
    use_distributed = should_use_distributed_mode(args, config)

    if use_distributed:
        logger.info("Using distributed execution mode with Redis")
        run_experiment_distributed(args, config)
    else:
        logger.info("Using local execution mode (no Redis)")
        run_experiment_local(args, config)


def should_use_distributed_mode(args, config) -> bool:
    """
    Determine if distributed mode should be used.

    Criteria for local mode:
    - Only 1 total trial (1 CRS × 1 benchmark × 1 trial)
    - redis_host not specified in config
    - Redis not available (connection check)
    - User explicitly requests local mode via CLI flag

    Returns:
        bool: True if should use distributed mode
    """
    # Calculate total number of jobs (from config)
    benchmarks = config.get_benchmark_list()
    crses = config.crses
    total_jobs = len(benchmarks) * len(crses) * config.trials

    # User explicitly disabled distributed mode
    if hasattr(args, 'local_only') and args.local_only:
        logger.info("Local mode explicitly requested via --local-only flag")
        return False

    # Only 1 job - use local mode by default
    if total_jobs == 1:
        logger.info(f"Single job detected ({total_jobs} jobs total), using local mode")
        return False

    # No Redis host configured
    if not config.redis_host or config.redis_host == "none":
        logger.info("No Redis host configured, using local mode")
        return False

    # Check if Redis is available
    if not check_redis_available(config.redis_host):
        logger.warning(f"Redis not available at {config.redis_host}, falling back to local mode")
        return False

    # Multiple jobs and Redis available - use distributed
    logger.info(f"Multiple jobs detected ({total_jobs} jobs total), using distributed mode")
    return True


def check_redis_available(redis_host: str) -> bool:
    """Check if Redis server is reachable."""
    try:
        import redis
        client = redis.Redis(host=redis_host, socket_connect_timeout=2)
        client.ping()
        return True
    except (ImportError, redis.ConnectionError, redis.TimeoutError):
        return False


def run_experiment_local(args, config) -> None:
    """
    Run experiment locally without Redis queue.

    Executes all trials sequentially in the current process.
    """
    logger.info("="*60)
    logger.info("Running CRSBench in Local Mode (No Redis)")
    logger.info("="*60)

    # CRSes and benchmarks from config YAML
    benchmarks = config.get_benchmark_list()
    crses = config.crses

    # Generate trial matrix
    trials = generate_trial_matrix(config)

    logger.info(f"Total trials to execute: {len(trials)}")
    logger.info(f"CRSes: {', '.join(crses)}")
    logger.info(f"Benchmarks: {', '.join(benchmarks)}")
    logger.info(f"Trials per combination: {config.trials}")
    logger.info("="*60)

    # Execute trials sequentially
    results = []
    for idx, trial in enumerate(trials, 1):
        logger.info(f"\n[{idx}/{len(trials)}] Starting trial:")
        logger.info(f"  CRS: {trial.crs}")
        logger.info(f"  Benchmark: {trial.benchmark}")
        logger.info(f"  Trial: {trial.trial_num}")

        # Import and execute job directly
        from crsbench.distributed.jobs import run_crs_trial

        result = run_crs_trial(
            crs=trial.crs,
            benchmark=trial.benchmark,
            trial_num=trial.trial_num,
            config=config.to_dict()
        )

        results.append(result)

        # Log result
        if result['success']:
            logger.info(f"  ✓ Success: {result['povs_found']}/{result['total_povs']} POVs found")
        else:
            logger.error(f"  ✗ Failed: {result.get('error', 'Unknown error')}")

    # Generate final report
    logger.info("\n" + "="*60)
    logger.info("Experiment Complete - Generating Report")
    logger.info("="*60)

    generate_final_report(results, args.experiment_name, config)
```

**CLI Flag Addition**:

```python
def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(...)

    # ... existing arguments ...

    parser.add_argument(
        '--local-only',
        action='store_true',
        help='Force local execution mode without Redis (useful for single jobs)'
    )

    return parser.parse_args()
```

**Configuration Update**:

Make `redis_host` optional in experiment config:

```yaml
# experiment-config.yaml

# For distributed execution
trials: 3
max_total_time: 86400
difficulty_level: 1
experiment_filestore: /tmp/crsbench/experiment-data
report_filestore: /tmp/crsbench/report-data
redis_host: queue-server  # OPTIONAL: Omit or set to "none" for local mode

# For local execution (single job)
trials: 1
max_total_time: 86400
difficulty_level: 1
experiment_filestore: /tmp/crsbench/experiment-data
report_filestore: /tmp/crsbench/report-data
# redis_host: none  # Explicit local mode, or simply omit this field
```

**Schema Update**:

```python
class ExperimentConfig(BaseModel):
    """Experiment configuration schema."""

    trials: int = Field(..., ge=1)
    max_total_time: int = Field(..., ge=1)
    difficulty_level: int = Field(..., ge=0, le=4)
    experiment_filestore: str = Field(...)
    report_filestore: str = Field(...)
    redis_host: Optional[str] = Field(default=None, description="Redis server hostname (optional, omit for local mode)")

    @validator('redis_host')
    def validate_redis_host(cls, v):
        if v and v.strip() and v.strip().lower() != 'none':
            return v.strip()
        return None  # Treat empty or "none" as None
```

**User Experience**:

```bash
# Single job - automatically uses local mode
# (config.yaml has 1 benchmark, 1 CRS, 1 trial)
crsbench run --experiment-config config.yaml

# Output:
# [INFO] Single job detected (1 jobs total), using local mode
# [INFO] Running CRSBench in Local Mode (No Redis)

# Multiple jobs - automatically uses distributed mode (if Redis available)
# (config.yaml has 3 benchmarks, 2 CRSes)
crsbench run --experiment-config config.yaml \


# Output:
# [INFO] Multiple jobs detected (6 jobs total), using distributed mode
# [INFO] Connecting to Redis at queue-server

# Force local mode even with multiple jobs
crsbench run --local-only \
             --experiment-config config.yaml \


# Output:
# [INFO] Local mode explicitly requested via --local-only flag
# [INFO] Running CRSBench in Local Mode (No Redis)
```

**Benefits**:
1. **Zero Infrastructure**: No Redis setup needed for simple cases
2. **Fast Iteration**: Quick testing during development
3. **Automatic Detection**: Intelligently chooses best mode
4. **Explicit Control**: `--local-only` flag for manual override
5. **Graceful Degradation**: Falls back to local if Redis unavailable

### 13.2 Backward Compatibility

- Existing single-threaded code preserved as `run_experiment_local()`
- No breaking changes to CLI interface
- Optional Redis dependency (can run without `redis` package for local mode)
- Configuration backward compatible (redis_host is optional)

```python
# Example: Graceful import of Redis dependencies
try:
    import redis
    import rq
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logger.debug("Redis/RQ not installed, distributed mode unavailable")

def should_use_distributed_mode(args, config) -> bool:
    """..."""
    if not REDIS_AVAILABLE:
        logger.info("Redis/RQ not installed, using local mode")
        return False
    # ... rest of logic
```

### 13.3 Gradual Rollout

**Phase 1**: Implement core infrastructure (this doc)
**Phase 2**: Test with small experiments
**Phase 3**: Scale to full benchmark suites
**Phase 4**: Add advanced features (auto-scaling, monitoring)

## 14. References

- RQ Documentation: https://python-rq.org/
- Redis Documentation: https://redis.io/documentation

## 15. Appendix

### 15.1 Environment Variables

| Variable | Description | Default | Used By |
|----------|-------------|---------|---------|
| CRSBENCH_REDIS_HOST | Redis server hostname | localhost | Orchestrator, Worker |
| PYTHONPATH | Python module path | /app | Worker |

### 15.2 Queue Naming Convention

Queue naming follows the runtime queue model:
- Flat default (`CRSBENCH_QUEUE_MODEL=flat`):
  - `crsbench_trial`
  - `crsbench_build`
  - `crsbench_verify`
- Legacy per-experiment (`CRSBENCH_QUEUE_MODEL=per-experiment`):
  - `crsbench_{experiment_name}`
  - `crsbench_{experiment_name}_build`
  - `crsbench_{experiment_name}_verify`

See `docs/design/distributed/configless-runtime.md` for canonical behavior.

### 15.3 File Locations

```
crsbench/
├── run_experiment.py                # Main CLI entry point (Orchestrator)
├── __init__.py                      # Package initialization
└── distributed/                     # Distributed job queue module
    ├── __init__.py                  # Module exports
    ├── jobs.py                      # Job definitions
    ├── worker.py                    # Worker implementation
    └── queue.py                     # Queue utilities

compose/
└── crsbench.yaml                    # Docker compose

docker/
└── worker/
    ├── Dockerfile                   # Worker container image
    └── startup-worker.sh            # Worker startup script

tests/
└── test_distributed/                # Tests for distributed module
    ├── __init__.py
    ├── test_jobs.py                 # Job tests
    ├── test_worker.py               # Worker tests
    └── test_queue.py                # Queue utilities tests

docs/design/
└── distributed-job-queue.md         # This document
```

**Module Organization Rationale**:
- **Only `run_experiment.py` at root**: Single entry point as per coding standards
- **`crsbench/distributed/` module**: All distributed execution code grouped together
- **Cohesive structure**: Related functionality (jobs, worker, queue) in one place
- **Clear separation**: Distributed features isolated from core evaluation logic

---

**Document Version**: 1.0
**Last Updated**: 2025-10-13
**Next Review**: After Phase 1 implementation
