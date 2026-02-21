# Multi-Machine Deployment Guide

**Date**: 2026-02-01
**Prerequisite**: Read [distributed-evaluation.md](distributed-evaluation.md) for architecture overview.

## Topology

A distributed CRSBench deployment consists of three process types sharing a single Redis instance:

```
  Machine A (Orchestrator + Worker)       Machine B (Worker)       Machine C (Evaluator)
  ┌─────────────────────────────┐   ┌───────────────────┐   ┌────────────────────────┐
  │  crsbench run               │   │  crsbench worker   │   │  crsbench evaluator    │
  │  crsbench worker (optional) │   │                    │   │                        │
  │  Redis server               │   │                    │   │                        │
  └──────────┬──────────────────┘   └────────┬───────────┘   └───────────┬────────────┘
             │                               │                           │
             └───────────┬───────────────────┘                           │
                         │                                               │
                    SSH tunnel to Redis (port 6379)                SSH tunnel to Redis
```

- **Machine A**: Runs Redis, the orchestrator (`crsbench run`), and optionally a worker.
- **Machine B..N**: Workers that process CRS trial jobs.
- **Machine C..M**: Evaluators that build variants and verify POVs. Evaluators are optional.

## Prerequisites

All machines need:

- CRSBench installed (`uv pip install -e .`)
- Docker running
- OSS-Fuzz repository cloned (same path or use `--oss-fuzz-path` override)
- Benchmarks available (same path or use `--benchmarks-root` override)
- Python packages: `redis`, `rq` (`pip install redis rq`)

Machine A additionally needs:

- Redis server installed and running (`apt install redis-server` or `brew install redis`)

## SSH Tunnel Setup

Workers and evaluators on remote machines connect to Redis on Machine A via SSH local port forwarding.

### On each remote machine (B, C, etc.)

```bash
# Forward local port 6379 to Machine A's Redis
ssh -N -L 6379:localhost:6379 user@machine-a &

# Verify connection
redis-cli ping
# Should return: PONG
```

The `-N` flag runs SSH without a shell. The `-L` flag forwards local port 6379 to `localhost:6379` on Machine A (where Redis is listening).

### Persistent tunnel with autossh

For long-running experiments, use `autossh` to auto-reconnect:

```bash
# Install autossh
apt install autossh  # or brew install autossh

# Start persistent tunnel
autossh -M 0 -f -N -L 6379:localhost:6379 user@machine-a \
  -o "ServerAliveInterval 30" -o "ServerAliveCountMax 3"
```

### Redis with password

If Redis requires authentication:

```bash
# Set on all machines (orchestrator, workers, evaluators)
export REDIS_PASSWORD=your-redis-password
```

### Verify tunnel works

```bash
# From remote machine
redis-cli -h localhost ping
# PONG

# Check queue contents
redis-cli -h localhost keys "crsbench_*"
```

## Running an Experiment

### Step 1: Start Redis (Machine A)

```bash
redis-server --daemonize yes
```

### Step 2: Create experiment config

```yaml
# experiment-config.yaml
experiment: my-distributed-exp
experiment_filestore: /data/experiments
oss_fuzz_path: /opt/oss-fuzz
benchmarks_root: /home/user/CRSBench/benchmarks
max_total_time: 3600
redis_host: localhost
benchmarks:
  - sanity-mock-c-delta-01
  - afc-curl-delta-01
crses:
  - my-crs
trials: 3
```

### Step 3: Start evaluator (Machine C, optional)

Start the evaluator first so variant images are ready before POVs arrive.

```bash
# Set up SSH tunnel first
ssh -N -L 6379:localhost:6379 user@machine-a &

# Run evaluator
crsbench evaluator \
  --experiment-config experiment-config.yaml \
  --experiment-name my-distributed-exp \
  --redis-host localhost
```

The evaluator:
1. Builds all variant Docker images for the listed benchmarks
2. Starts listening on the `crsbench_my-distributed-exp_verify` queue
3. Processes POV verification jobs as they arrive

### Step 4: Start workers (Machine B..N)

```bash
# Set up SSH tunnel first
ssh -N -L 6379:localhost:6379 user@machine-a &

# Run worker (paths come from experiment config YAML worker: section)
crsbench worker \
  --experiment-config experiment-config.yaml \
  --experiment-name my-distributed-exp \
  --redis-host localhost
```

### Step 5: Start orchestrator (Machine A)

```bash
# benchmarks, crses configured in experiment-config.yaml
crsbench run \
  --experiment-config experiment-config.yaml \
  --distributed
```

The orchestrator enqueues trial jobs to `crsbench_my-distributed-exp`. Workers pick them up, run CRS trials, and enqueue POV verification to `crsbench_my-distributed-exp_verify`. Evaluators process the verification queue.

### Step 6: Monitor progress

```bash
# On Machine A (or any machine with tunnel)
# Check queue depths
redis-cli llen crsbench_my-distributed-exp
redis-cli llen crsbench_my-distributed-exp_verify

# Check registered workers
redis-cli smembers rq:workers
```

## Worker Config Overrides

In distributed mode, the orchestrator serializes the full experiment config
into each Redis job. Workers deserialize it, but the paths (oss_fuzz_path,
benchmarks_root, etc.) reflect the orchestrator's filesystem — which may
differ from the worker's.

The `worker:` section in the experiment YAML provides machine-specific
overrides. Since this section is included in the serialized config, workers
apply it automatically at job execution time. All workers sharing the same
config get the same overrides — for heterogeneous clusters where each worker
needs different paths, use shared storage with consistent mount points.

### Worker config override example

```yaml
# Experiment config with worker overrides
oss_fuzz_path: /home/orchestrator/oss-fuzz    # orchestrator's path
benchmarks_root: /home/orchestrator/benchmarks

worker:
  oss_fuzz_path: /opt/oss-fuzz                # worker's path
  benchmarks_root: /data/benchmarks
  experiment_filestore: /data/experiments
```

### Evaluator CLI overrides

Evaluators accept CLI flags to override config paths directly:

```bash
crsbench evaluator --experiment-config config.yaml \
  --oss-fuzz-path /opt/oss-fuzz \
  --benchmarks-root /data/benchmarks
```

## Distributed CI Builds

The `crsbench benchmark ci` command supports distributing builds to remote evaluators:

```bash
# On Machine A (with Redis running)
crsbench benchmark ci build --all \
  --distributed \
  --redis-host localhost

# On Machine B (with SSH tunnel)
crsbench evaluator --ci --redis-host localhost \
  --build-jobs 8 --build-cores-per-job 4 \
  --verify-cores-per-job 2 \
  --continuous
```

The submitter enqueues jobs to Redis build/verify queues. Evaluators running `crsbench evaluator --ci` dequeue and execute them. See `docs/modules/benchmark-ci.md` for full worker options.

## Post-Experiment Evaluation

If no evaluator was running during the experiment, verification jobs accumulate in Redis. Run an evaluator after the experiment to process them:

```bash
# After experiment completes, start evaluator to drain verify queue
crsbench evaluator \
  --experiment-config experiment-config.yaml \
  --experiment-name my-distributed-exp
```

The evaluator builds variants, then processes all queued verification jobs.

## Troubleshooting

### Redis connection refused

```
Error: Could not connect to Redis at localhost:6379
```

- Verify Redis is running: `redis-cli ping` on Machine A
- Verify SSH tunnel is active: `ss -tlnp | grep 6379` on remote machine
- Check firewall rules on Machine A

### Docker image not found

```
Error: No such image: aixcc-afc-...
```

- Evaluators build their own images locally. Each evaluator must build before verifying.
- Workers build CRS images via `oss-crs prepare` / `oss-crs build-target`. Ensure Docker is running.
- Check that `oss-fuzz` path points to a valid OSS-Fuzz directory with the project.

### Worker override paths

```
Error: Benchmark not found: /home/orchestrator/benchmarks/...
```

- The serialized config contains the orchestrator's paths. Workers on different machines need overrides.
- Set `benchmarks_root`, `oss_fuzz_path`, etc. in the `worker:` section of the experiment YAML.

### Evaluator not processing jobs

- Verify the experiment name matches: `crsbench_{experiment_name}_verify`
- Check that the evaluator's variant build succeeded (check logs)
- Verify Redis connectivity from the evaluator machine
