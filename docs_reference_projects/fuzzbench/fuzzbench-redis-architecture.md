# FuzzBench Redis-Based Server-Worker Architecture

## Overview

FuzzBench uses **Redis** as a job queue backend with the **RQ (Redis Queue)** library to coordinate distributed work across multiple worker processes. This enables scalable, parallel execution of fuzzing experiments with proper job dependency management.

## Architecture Components

### 1. Queue Server (Redis)

- **Technology**: Redis 4.3.4
- **Service**: Runs as a standalone Docker container named `queue-server`
- **Purpose**: Central message broker and job queue storage
- **Location**: compose/fuzzbench.yaml:27-28

```yaml
queue-server:
  image: redis
```

### 2. Experiment Orchestrator (Server/Producer)

**File**: fuzzbench/run_experiment.py

**Responsibilities**:
- Creates RQ queue for job scheduling
- Enqueues build and run jobs with dependencies
- Monitors job status continuously
- Waits for all jobs to complete

**Key Implementation**:

```python
def main():
    """Set up Redis connection and start the experiment."""
    redis_connection = redis.Redis(host='queue-server')

    with rq.Connection(redis_connection):
        return run_experiment(config)

def run_experiment(config):
    """Main experiment logic."""
    # Create the queue for scheduling build jobs and run jobs
    queue = rq.Queue('build_n_run_queue')

    images_to_build = docker_images.get_images_to_build(
        config['fuzzers'], config['benchmarks']
    )

    jobs_list = []
    for name, image in images_to_build.items():
        depends = image.get('depends_on', None)
        jobs_list.append(
            queue.enqueue(
                jobs.build_image,
                image=image,
                job_timeout=30 * 60,  # 30 minutes
                result_ttl=-1,  # Keep results forever
                job_id=name,
                depends_on=depends[0] if 'depends_on' in image else None
            )
        )

    # Poll job status continuously
    while True:
        print(f'queued:\t{queue.count}')
        print(f'started:\t{queue.started_job_registry.count}')
        print(f'deferred:\t{queue.deferred_job_registry.count}')
        print(f'finished:\t{queue.finished_job_registry.count}')
        print(f'failed:\t{queue.failed_job_registry.count}')

        if all([job.result is not None for job in jobs_list]):
            break
        time.sleep(3)
```

**Location**: fuzzbench/run_experiment.py:26-82

### 3. Workers (Consumers)

**File**: fuzzbench/worker.py

**Responsibilities**:
- Connect to Redis queue server
- Poll for available jobs in the queue
- Execute jobs (build images, run trials, etc.)
- Report results back to queue
- Terminate when all work is complete

**Key Implementation**:

```python
def main():
    """Sets up Redis connection and starts the worker."""
    redis_connection = redis.Redis(host='queue-server')

    with rq.Connection(redis_connection):
        queue = rq.Queue('build_n_run_queue')
        worker = rq.Worker([queue])

        # Work in burst mode until queue is empty
        while queue.count + queue.deferred_job_registry.count > 0:
            worker.work(burst=True)
            time.sleep(5)
```

**Location**: fuzzbench/worker.py:21-34

**Docker Compose Configuration**:

```yaml
worker:
  image: fuzzbench
  environment:
    RQ_REDIS_URL: redis://queue-server
    PYTHONPATH: .
  command: python3 fuzzbench/worker.py
  volumes:
    - /var/run/docker.sock:/var/run/docker.sock  # Access to Docker daemon
  links:
    - queue-server
  depends_on:
    - run-experiment
```

**Location**: compose/fuzzbench.yaml:13-26

### 4. Queue Utilities (Helper Module)

**File**: common/queue_utils.py

**Purpose**: Centralized queue initialization and job management

**Key Functions**:

```python
def initialize_queue(redis_host):
    """Returns a redis-backed rq queue."""
    queue_name = experiment_utils.get_experiment_name()
    redis_connection = redis.Redis(host=redis_host)
    queue = rq.Queue(queue_name, connection=redis_connection)
    return queue

def get_all_jobs(queue):
    """Returns all the jobs in queue."""
    job_ids = queue.get_job_ids()
    return rq.job.Job.fetch_many(job_ids, queue.connection)
```

**Location**: common/queue_utils.py:22-33

## Key Features

### 1. Job Dependency Management

Jobs can declare dependencies on other jobs using `depends_on` parameter:

```python
queue.enqueue(
    jobs.build_image,
    image=image,
    job_id='fuzzer-benchmark-pair',
    depends_on='base-image-job-id'  # Will wait for this to complete
)
```

**Location**: fuzzbench/run_experiment.py:43-49

### 2. Job Status Tracking

RQ provides multiple registries to track job lifecycle:

- `queue.count` - Jobs waiting in queue
- `queue.started_job_registry.count` - Jobs currently executing
- `queue.deferred_job_registry.count` - Jobs waiting for dependencies
- `queue.finished_job_registry.count` - Successfully completed jobs
- `queue.failed_job_registry.count` - Failed jobs

**Location**: fuzzbench/run_experiment.py:51-57

### 3. Burst Mode Processing

Workers use `burst=True` to process jobs and exit when queue is empty:

```python
worker.work(burst=True)  # Process one job then return
```

This is useful for:
- Local experiments with limited duration
- CI/CD environments
- Cost control (workers can scale down)

**Location**: fuzzbench/worker.py:29

### 4. Cloud Worker Scheduling

**File**: experiment/schedule_measure_workers.py

For cloud deployments, FuzzBench dynamically scales GCE instances based on queue depth:

```python
def schedule(experiment_config: dict, queue):
    """Schedule measurer workers based on queue size."""
    jobs = queue_utils.get_all_jobs(queue)
    counts = collections.defaultdict(int)
    for job in jobs:
        counts[job.get_status(refresh=False)] += 1

    # Scale instances to match work
    num_instances_needed = counts['queued'] + counts['started']
    num_instances_needed = min(num_instances_needed, MAX_INSTANCES_PER_GROUP)

    # Resize GCE instance group
    gce.resize_instance_group(num_instances_needed, instance_group_name,
                             project, zone)
```

**Location**: experiment/schedule_measure_workers.py:101-137

Workers receive Redis connection info via environment:

```python
env = {
    'REDIS_HOST': redis_host,
    'EXPERIMENT_FILESTORE': experiment_filestore,
    'EXPERIMENT': experiment,
}
```

**Location**: experiment/schedule_measure_workers.py:69-75

## Job Types

Jobs are defined in fuzzbench/jobs.py and include:

1. `build_image(image)` - Build Docker images for fuzzer-benchmark pairs
2. `run_trial()` - Execute fuzzing trials
3. `measure_corpus_snapshot()` - Measure code coverage

**Location**: fuzzbench/jobs.py:22-45

## Dependencies

```
redis==4.3.4  # Redis Python client
rq==1.11.1    # Redis Queue job processing library
```

**Location**: requirements.txt:20-21

## Communication Flow

```
┌─────────────────────┐
│  run_experiment.py  │  (Producer/Server)
│                     │
│  1. Connect to      │
│     Redis           │
│  2. Create queue    │
│  3. Enqueue jobs    │
│     with deps       │
│  4. Monitor status  │
└──────────┬──────────┘
           │
           │ Redis Protocol
           ▼
    ┌──────────────┐
    │ queue-server │  (Redis Container)
    │              │
    │ - Job queue  │
    │ - Job status │
    │ - Results    │
    └──────┬───────┘
           │
           │ Redis Protocol
           ▼
┌─────────────────────┐
│    worker.py        │  (Consumer × N)
│                     │
│  1. Connect to      │
│     Redis           │
│  2. Poll queue      │
│  3. Execute jobs    │
│  4. Return results  │
│  5. Repeat          │
└─────────────────────┘
```

## Advantages of This Architecture

### Scalability
- Horizontal scaling by adding more worker containers
- Cloud deployments can auto-scale based on queue depth
- No worker limit (up to 1000 GCE instances per group)

### Reliability
- Jobs persist in Redis if workers crash
- Failed jobs tracked separately in `failed_job_registry`
- Configurable timeouts prevent hanging jobs

### Simplicity
- RQ provides high-level Python API over Redis
- No need to implement custom job queue logic
- Built-in job status tracking and management

### Dependency Management
- Jobs can depend on other jobs completing first
- Deferred jobs automatically scheduled when dependencies complete
- Handles complex build dependency graphs

### Flexibility
- Works in both local (docker-compose) and cloud (GCE) environments
- Workers can be stateless and ephemeral
- Easy to add new job types

## Relevant for CRSBench

This architecture pattern is well-suited for CRSBench because:

1. **Parallel CRS Execution**: Multiple CRS instances can run concurrently
2. **Trial Management**: Each trial can be a separate job
3. **Resource Scaling**: Workers can scale based on workload
4. **Fault Tolerance**: Jobs persist if CRS crashes or times out
5. **Progress Tracking**: Real-time visibility into experiment progress
6. **Dependency Handling**: POV validation can depend on exploit generation
7. **Heterogeneous Workers**: Different worker types for different CRS architectures

## Implementation Recommendations for CRSBench

### Similar Pattern
```python
# crsbench/queue_utils.py
def initialize_queue(redis_host):
    queue_name = get_experiment_name()
    redis_connection = redis.Redis(host=redis_host)
    return rq.Queue(queue_name, connection=redis_connection)

# crsbench/run_experiment.py
def run_experiment(config):
    queue = rq.Queue('crsbench_trials')

    # Enqueue CRS trials
    for crs in config['crses']:
        for benchmark in config['benchmarks']:
            for trial_num in range(config['trials']):
                queue.enqueue(
                    run_crs_trial,
                    crs=crs,
                    benchmark=benchmark,
                    trial_num=trial_num,
                    timeout=config['max_total_time']
                )

# crsbench/worker.py
def main():
    redis_connection = redis.Redis(host=os.environ['REDIS_HOST'])
    with rq.Connection(redis_connection):
        queue = rq.Queue('crsbench_trials')
        worker = rq.Worker([queue])
        worker.work()
```

### Key Adaptations
- Use environment variable for Redis host (support both local and cloud)
- Implement timeout enforcement for CRS trials
- Store results in both Redis (immediate) and persistent storage (long-term)
- Support multiple queue types (trials, evaluation, reporting)
- Add monitoring endpoints for experiment progress
