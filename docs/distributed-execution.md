# Distributed Execution Guide

This guide explains how to run CRSBench experiments in distributed mode using multiple workers for parallel trial execution.

## Overview

CRSBench supports two execution modes:

- **Local Mode**: Sequential execution of trials on a single machine
- **Distributed Mode**: Parallel execution across multiple worker processes using Redis queue

Distributed mode enables horizontal scaling for large experiments with many trials.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                   CRSBench Orchestrator                     │
│                  (crsbench command)                         │
│                                                             │
│  • Generates trial matrix (CRS × Benchmark × Trials)       │
│  • Enqueues jobs to Redis                                  │
│  • Monitors job progress                                   │
│  • Collects results                                        │
└──────────────────┬──────────────────────────────────────────┘
                   │
                   │ Enqueue jobs
                   ▼
            ┌──────────────┐
            │ Redis Server │
            │  (Queue)     │
            └──────┬───────┘
                   │
                   │ Dequeue jobs
                   ▼
    ┌──────────────────────────────────────────┐
    │         Workers (× N)                    │
    │                                          │
    │  Worker 1: Execute CRS trial             │
    │  Worker 2: Execute CRS trial             │
    │  Worker 3: Execute CRS trial             │
    │  ...                                     │
    │  Worker N: Execute CRS trial             │
    └──────────────────────────────────────────┘
```

## Prerequisites

### Install Redis Dependencies

```bash
# Install Redis and RQ (Python Redis Queue)
uv pip install redis rq

# Or add to your environment
pip install redis rq
```

### Start Redis Server

#### Option 1: Local Redis (Development)

**Ubuntu/Debian:**
```bash
# Update package list
sudo apt update

# Install Redis
sudo apt install redis-server

# Start Redis service
sudo systemctl start redis-server

# Enable Redis to start on boot
sudo systemctl enable redis-server

# Verify Redis is running
redis-cli ping
# Expected output: PONG

# Check Redis status
sudo systemctl status redis-server
```

**Arch Linux:**
```bash
# Install Valkey (Redis alternative)
sudo pacman -S valkey

# Start Valkey service
sudo systemctl start valkey

# Enable Valkey to start on boot
sudo systemctl enable valkey

# Verify Valkey is running
valkey-cli ping
# Expected output: PONG
```

**Manual Start (any Linux):**
```bash
# Start Redis server in foreground
redis-server

# Or start in background with config
redis-server /etc/redis/redis.conf --daemonize yes

# Stop Redis
redis-cli shutdown
```

#### Option 2: Docker Valkey (Recommended)

```bash
# Run Valkey in Docker
docker run -d \
  --name crsbench-valkey \
  -p 6379:6379 \
  valkey/valkey:8.0-alpine

# Verify Valkey is running
docker exec crsbench-valkey valkey-cli ping
# Expected output: PONG
```

#### Option 3: Docker Compose (Production)

**Recommended**: Use the provided docker-compose configuration in `services/valkey/`:

```bash
# Start Valkey service
docker-compose -f services/valkey/docker-compose.yml up -d

# Verify it's running
docker exec crsbench-valkey valkey-cli ping
```

Or create your own `docker-compose.yml`:

```yaml
version: "3.8"

services:
  valkey:
    image: valkey/valkey:8.0-alpine
    container_name: crsbench-valkey
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "valkey-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5
    volumes:
      - valkey-data:/data
    restart: unless-stopped
    command: valkey-server --appendonly yes

volumes:
  valkey-data:
```

Start Valkey:

```bash
docker-compose up -d
```

## Configuration

### Experiment Config File

Add `redis_host` to your experiment configuration to enable distributed mode:

```yaml
# experiment-config.yaml
experiment: my-distributed-exp
trials: 3
max_total_time: 7200
difficulty_level: 1
experiment_filestore: /tmp/experiments
report_filestore: /tmp/reports

# Distributed execution configuration
redis_host: localhost  # Redis server hostname

# CRS and benchmark configuration
crses:
  - atlantis-c
  - atlantis-multilang
benchmarks:
  - libjpeg-turbo
  - libxml2
```

### Redis Host Configuration

| Setting | Usage | Example |
|---------|-------|---------|
| `redis_host: localhost` | Local Redis on same machine | Development |
| `redis_host: redis` | Docker Compose service name | Docker deployment |
| `redis_host: 10.0.1.5` | Remote Redis IP address | Cloud/cluster |
| `redis_host: none` or omit | Disable distributed mode | Local execution |

## Valkey Cleanup (Important!)

**Before running a new experiment**, it's recommended to clean up Valkey to avoid conflicts with previous experiments.

### Quick Cleanup with Helper Script (Recommended)

```bash
# Clean specific experiment
python scripts/valkey-helper.py clean my-old-exp

# Or clean all experiments (with confirmation)
python scripts/valkey-helper.py clean-all

# Check what's in the queues
python scripts/valkey-helper.py list-queues
```

### Manual Cleanup Options

**Option 1: Clean Specific Experiment Queue**

```bash
# Connect to Valkey
valkey-cli

# Delete specific experiment queue
> DEL rq:queue:crsbench_my-old-exp

# Delete all keys related to specific experiment
> KEYS rq:*crsbench_my-old-exp*
# Then delete each key shown
> DEL rq:queue:crsbench_my-old-exp
> DEL rq:finished:crsbench_my-old-exp
> DEL rq:failed:crsbench_my-old-exp
> DEL rq:started:crsbench_my-old-exp

# Exit Valkey CLI
> exit
```

**One-liner to clean specific experiment:**
```bash
valkey-cli KEYS "rq:*crsbench_my-old-exp*" | xargs valkey-cli DEL
```

**Option 2: Flush Entire Database (Use with Caution!)**

**WARNING**: This deletes ALL data in Valkey, including data from other applications!

```bash
# Using helper script (with confirmation)
python scripts/valkey-helper.py clean-all

# Or manually
valkey-cli FLUSHDB    # Flush current database
valkey-cli FLUSHALL   # Flush ALL databases
```

**Option 3: Restart Valkey (Complete Cleanup)**

**For development/testing** - this ensures a completely clean state:

```bash
# Using helper script
python scripts/valkey-helper.py restart

# If using systemd (Ubuntu/Debian/Arch)
sudo systemctl restart redis-server  # Ubuntu/Debian
sudo systemctl restart valkey         # Arch

# If using Docker
docker restart crsbench-valkey

# Or stop and remove Docker container
docker stop crsbench-valkey
docker rm crsbench-valkey
# Then start fresh
docker run -d --name crsbench-valkey -p 6379:6379 valkey/valkey:8.0-alpine
```

### When to Clean Valkey

**Always clean before:**
- Starting a new experiment with the same name
- Re-running a failed/interrupted experiment
- Switching between different experiment configurations

**No need to clean if:**
- Using a unique experiment name each time
- Running completely independent experiments

### Verify Valkey is Clean

**Using Helper Script:**
```bash
python scripts/valkey-helper.py list-queues  # Should show no queues
python scripts/valkey-helper.py stats        # Check database size
```

**Manual Commands:**
```bash
# Check if specific experiment queue exists
valkey-cli EXISTS rq:queue:crsbench_my-exp
# Output: 0 = doesn't exist (clean)
# Output: 1 = exists (has data)

# List all CRSBench queues
valkey-cli KEYS "rq:*crsbench_*"

# Count total keys
valkey-cli DBSIZE
```

## Running Distributed Experiments

> **Quick Start**: Use the helper script for easier Valkey management:
> ```bash
> python scripts/valkey-helper.py start
> python scripts/valkey-helper.py status
> ```
> See [scripts/README.md](../scripts/README.md) for complete documentation.

### Step 1: Start Valkey Server

**Option A: Using Helper Script (Recommended for Testing)**

```bash
# Start Valkey
python scripts/valkey-helper.py start

# Verify it's running
python scripts/valkey-helper.py status
```

**Option B: Manual Docker Commands**

```bash
# Using Docker
docker run -d --name crsbench-valkey -p 6379:6379 valkey/valkey:8.0-alpine

# Or using Docker Compose
docker-compose -f services/valkey/docker-compose.yml up -d

# Verify
valkey-cli ping
```

### Step 2: Start Workers

Open **multiple terminal windows** (or use `tmux`/`screen`) and start workers:

#### Terminal 1: Worker 1
```bash
# Set environment variables
export REDIS_HOST=localhost
export EXPERIMENT_NAME=my-distributed-exp

# Start worker
python -m crsbench.distributed.worker
```

#### Terminal 2: Worker 2
```bash
export REDIS_HOST=localhost
export EXPERIMENT_NAME=my-distributed-exp

python -m crsbench.distributed.worker
```

#### Terminal 3: Worker 3
```bash
export REDIS_HOST=localhost
export EXPERIMENT_NAME=my-distributed-exp

python -m crsbench.distributed.worker
```

**Note**: Start as many workers as you want for parallelism. Each worker will execute one trial at a time.

### Step 3: Run Experiment (Orchestrator)

In a separate terminal, run the experiment orchestrator:

```bash
crsbench \
  --experiment-config experiment-config.yaml \
  --experiment-name my-distributed-exp \
  --crses atlantis-c,atlantis-multilang \
  --benchmarks libjpeg-turbo,libxml2
```

The orchestrator will:
1. Generate trial matrix (2 CRS × 2 benchmarks × 3 trials = 12 jobs)
2. Enqueue all 12 jobs to Redis
3. Monitor job progress in real-time
4. Collect results as jobs complete
5. Generate final report when all jobs finish

### Step 4: Monitor Progress

The orchestrator provides real-time progress updates:

```
============================================================
Running CRSBench in Distributed Mode (Redis)
============================================================
Redis host: localhost
Total jobs: 12

Enqueuing trial atlantis-c × libjpeg-turbo × trial 0
Enqueuing trial atlantis-c × libjpeg-turbo × trial 1
Enqueuing trial atlantis-c × libjpeg-turbo × trial 2
Enqueuing trial atlantis-c × libxml2 × trial 0
...

Job Progress: 3/12 completed (25.0%)
  ✓ atlantis-c × libjpeg-turbo × trial 0
  ✓ atlantis-c × libjpeg-turbo × trial 1
  ✓ atlantis-multilang × libjpeg-turbo × trial 0
  ⏳ atlantis-c × libjpeg-turbo × trial 2 (running)
  ⏳ atlantis-c × libxml2 × trial 0 (running)
  ⏳ atlantis-multilang × libjpeg-turbo × trial 1 (running)

All jobs completed! Generating final report...
```

## Advanced Usage

### Using Docker Compose for Everything

Create `docker-compose.yml`:

```yaml
version: "3.8"

services:
  valkey:
    image: valkey/valkey:8.0-alpine
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "valkey-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  orchestrator:
    build: .
    depends_on:
      valkey:
        condition: service_healthy
    environment:
      - REDIS_HOST=valkey
      - EXPERIMENT_NAME=${EXPERIMENT_NAME:-experiment}
    volumes:
      - ./experiments:/tmp/experiments
      - ./reports:/tmp/reports
      - ./experiment-config.yaml:/config/experiment-config.yaml
    command: >
      crsbench
      --experiment-config /config/experiment-config.yaml
      --experiment-name ${EXPERIMENT_NAME}
      --crses ${CRSES}
      --benchmarks ${BENCHMARKS}

  worker:
    build: .
    depends_on:
      - valkey
      - orchestrator
    environment:
      - REDIS_HOST=valkey
      - EXPERIMENT_NAME=${EXPERIMENT_NAME:-experiment}
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock  # For CRS Docker execution
      - ./experiments:/tmp/experiments
      - ./reports:/tmp/reports
    command: python -m crsbench.distributed.worker
    deploy:
      replicas: 4  # Run 4 workers in parallel
```

Run with:

```bash
# Set experiment parameters
export EXPERIMENT_NAME=my-exp
export CRSES=atlantis-c,atlantis-multilang
export BENCHMARKS=libjpeg-turbo,libxml2

# Start all services
docker-compose up

# Scale workers dynamically
docker-compose up --scale worker=8
```

### Worker Environment Variables

| Variable | Description | Default | Example |
|----------|-------------|---------|---------|
| `REDIS_HOST` | Redis server hostname | `localhost` | `redis`, `10.0.1.5` |
| `EXPERIMENT_NAME` | Experiment identifier (for queue naming) | `default` | `my-exp` |
| `WORKER_TIMEOUT` | Job execution timeout (seconds) | `3600` | `7200` |
| `LOG_LEVEL` | Logging verbosity | `INFO` | `DEBUG`, `WARNING` |

### Running Workers on Multiple Machines

For distributed execution across multiple physical machines:

**Machine 1 (Valkey + Orchestrator):**
```bash
# Start Valkey
docker run -d --name valkey -p 6379:6379 valkey/valkey:8.0-alpine

# Run orchestrator
crsbench \
  --experiment-config experiment-config.yaml \
  --experiment-name cluster-exp \
  --crses atlantis-c \
  --benchmarks bench1,bench2,bench3
```

**Machine 2 (Workers):**
```bash
# Point to Machine 1's Valkey
export REDIS_HOST=10.0.1.100  # IP of Machine 1
export EXPERIMENT_NAME=cluster-exp

# Start 4 workers
for i in {1..4}; do
  python -m crsbench.distributed.worker &
done
```

**Machine 3 (Workers):**
```bash
export REDIS_HOST=10.0.1.100
export EXPERIMENT_NAME=cluster-exp

# Start 4 more workers
for i in {1..4}; do
  python -m crsbench.distributed.worker &
done
```

Now you have 8 workers across 2 machines processing trials in parallel!

## Choosing Number of Workers

### Guidelines

- **Local Development**: 1-2 workers (matches CPU cores)
- **Single Machine**: N-1 workers (where N = number of CPU cores)
- **Multi-Machine Cluster**: 1-4 workers per machine depending on CRS resource usage
- **Cloud Deployment**: Scale based on queue depth and cost constraints

### Example Scenarios

**Scenario 1: 12 trials, 4 workers**
- Workers process trials in parallel
- Completion time: ~3× faster than sequential
- Resource usage: 4× CPU/memory

**Scenario 2: 100 trials, 10 workers**
- Workers continuously pull jobs from queue
- Completion time: ~10× faster than sequential
- Each worker processes ~10 trials sequentially

**Scenario 3: 1000 trials, 50 workers (cloud)**
- Horizontal scaling across multiple machines
- Completion time: ~50× faster than sequential
- Elastic scaling: add/remove workers dynamically

## Monitoring and Debugging

### Check Redis Queue Status

```bash
# Using redis-cli
redis-cli

# Check queue length
> LLEN rq:queue:crsbench_my-exp
12

# Check job IDs
> LRANGE rq:queue:crsbench_my-exp 0 -1

# Check worker status
> SMEMBERS rq:workers
```

### Using RQ Dashboard (Optional)

Install RQ dashboard for web-based monitoring:

```bash
pip install rq-dashboard

# Start dashboard
rq-dashboard --redis-url redis://localhost:6379

# Open browser: http://localhost:9181
```

The dashboard shows:
- Active workers
- Queued jobs
- Running jobs
- Completed/failed jobs
- Real-time job progress

### Worker Logs

Workers log all activity to stdout:

```
2025-01-15 10:30:00 - crsbench.distributed.worker - INFO - ============================================================
2025-01-15 10:30:00 - crsbench.distributed.worker - INFO - CRSBench Distributed Worker
2025-01-15 10:30:00 - crsbench.distributed.worker - INFO - ============================================================
2025-01-15 10:30:00 - crsbench.distributed.worker - INFO - Redis host: localhost
2025-01-15 10:30:00 - crsbench.distributed.worker - INFO - Experiment: my-exp
2025-01-15 10:30:00 - crsbench.distributed.worker - INFO - Worker timeout: 3600s
2025-01-15 10:30:00 - crsbench.distributed.worker - INFO - ============================================================
2025-01-15 10:30:01 - crsbench.distributed.worker - INFO - ✓ Connected to Redis successfully
2025-01-15 10:30:01 - crsbench.distributed.worker - INFO - Worker started, listening on queue: crsbench_my-exp
2025-01-15 10:30:01 - crsbench.distributed.worker - INFO - Waiting for jobs...
2025-01-15 10:30:05 - crsbench.distributed.jobs - INFO - Running trial 0 for atlantis-c on libjpeg-turbo
2025-01-15 10:35:12 - crsbench.distributed.jobs - INFO - Trial completed successfully
```

### Troubleshooting

**Problem: Workers not picking up jobs**

Check:
1. Redis is running: `redis-cli ping`
2. Workers using correct Redis host: Check `REDIS_HOST` env var
3. Workers using correct experiment name: Check `EXPERIMENT_NAME` env var
4. Queue name matches: `rq:queue:crsbench_{EXPERIMENT_NAME}`

**Problem: "Redis not available" error**

```bash
# Test Redis connection
redis-cli -h localhost ping

# Check Redis port is open
telnet localhost 6379

# Check firewall rules (if using remote Redis)
# Ensure port 6379 is open
```

**Problem: Jobs failing silently**

```bash
# Check failed job registry
redis-cli
> SMEMBERS rq:failed:crsbench_my-exp

# Inspect specific failed job
> HGETALL rq:job:{job-id}
```

**Problem: Workers exiting immediately**

- Workers run in "burst mode" - they exit when queue is empty
- This is normal behavior
- For continuous workers, use `run_worker_continuous()` function instead

## Best Practices

### 1. Use Unique Experiment Names

Always use unique experiment names to avoid queue conflicts:

```bash
# Good
crsbench --experiment-name exp-2025-01-15-v1 ...

# Bad (may conflict with previous runs)
crsbench --experiment-name test ...
```

### 2. Clean Up Redis Between Experiments

```bash
# Flush specific experiment queue
redis-cli DEL rq:queue:crsbench_old-exp

# Or flush entire Redis database (use with caution!)
redis-cli FLUSHDB
```

### 3. Match Worker Count to Workload

- Too few workers: Underutilized resources
- Too many workers: Resource contention, diminishing returns

Rule of thumb: 1 worker per 2-4 CPU cores

### 4. Use Persistent Storage for Results

Mount shared volumes for experiment results:

```yaml
volumes:
  - ./experiments:/tmp/experiments
  - ./reports:/tmp/reports
```

This ensures results persist even if containers restart.

### 5. Monitor Resource Usage

```bash
# Check worker CPU/memory
htop

# Check Docker resource usage
docker stats

# Adjust worker count if:
# - CPU usage < 50%: Add more workers
# - CPU usage > 90%: Reduce workers
# - Memory swapping: Reduce workers
```

## Comparison: Local vs Distributed Mode

| Feature | Local Mode | Distributed Mode |
|---------|------------|------------------|
| **Setup** | No setup required | Requires Redis |
| **Execution** | Sequential | Parallel |
| **Trials** | 1 at a time | N workers × 1 trial each |
| **Speed** | Baseline | Up to N× faster |
| **Resource** | Single process | Multiple processes |
| **Monitoring** | Simple | RQ dashboard available |
| **Best for** | Development, small experiments | Production, large experiments |

**When to use Local Mode:**
- Single trial execution
- Development/testing
- Limited resources
- Debugging CRS issues

**When to use Distributed Mode:**
- Multiple trials (>5)
- Production experiments
- Cloud deployments
- Need faster completion time

## Example Workflows

### Workflow 1: Development (Local Mode)

```bash
# Test with single trial, no Redis
crsbench \
  --experiment-config config.yaml \
  --experiment-name dev-test \
  --crses atlantis-c \
  --benchmarks test-benchmark \
  --local-only
```

### Workflow 2: Small Experiment (Local Valkey)

```bash
# Start Valkey with Docker
docker run -d --name crsbench-valkey -p 6379:6379 valkey/valkey:8.0-alpine

# Start 2 workers
python -m crsbench.distributed.worker &
python -m crsbench.distributed.worker &

# Run experiment (6 trials = 2 CRS × 1 benchmark × 3 trials)
crsbench \
  --experiment-config config.yaml \
  --experiment-name small-exp \
  --crses atlantis-c,atlantis-multilang \
  --benchmarks libjpeg-turbo
```

### Workflow 3: Large Experiment (Docker Compose)

```bash
# Create experiment config
cat > experiment-config.yaml <<EOF
experiment: large-exp
trials: 5
max_total_time: 14400
difficulty_level: 2
redis_host: valkey
experiment_filestore: /experiments
report_filestore: /reports
crses:
  - atlantis-c
  - atlantis-multilang
  - patchagent
benchmarks:
  - libjpeg-turbo
  - libxml2
  - libpng
  - zlib
EOF

# Start Valkey service
docker-compose -f services/valkey/docker-compose.yml up -d

# Start with 8 workers
docker-compose up --scale worker=8

# Total trials: 3 CRS × 4 benchmarks × 5 trials = 60 trials
# With 8 workers: ~8× speedup
```

## Cleanup

```bash
# Stop workers (Ctrl+C in each terminal)

# Stop Valkey
docker stop crsbench-valkey
docker rm crsbench-valkey

# Or with Docker Compose
docker-compose -f services/valkey/docker-compose.yml down

# Clean up Valkey data
docker volume rm valkey_valkey-data
```

## See Also

- [Design Document: Distributed Job Queue](../design-docs/distributed/distributed-job-queue.md) - Technical architecture
- [Snapshot System Guide](snapshot-examples.md) - Progress monitoring during trials
- [Experiment Configuration](benchmark-spec.md) - Configuration file format
