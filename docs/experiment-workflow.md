# Experiment Workflow

End-to-end guide for running CRSBench experiments: start Valkey, launch workers, run experiment, optionally evaluate POVs, and generate reports.

## Architecture

CRSBench uses a three-process model backed by a Redis-compatible queue (Valkey):

```
┌─────────────────────────────────────────────────────────────┐
│                   CRSBench Orchestrator                     │
│                  (crsbench run)                             │
│                                                             │
│  • Generates trial matrix (CRS × Benchmark × Trials)       │
│  • Enqueues trial jobs to Redis                            │
│  • Monitors job progress                                   │
│  • Collects results                                        │
└──────────────────┬──────────────────────────────────────────┘
                   │
                   │ Enqueue trial jobs
                   ▼
            ┌──────────────┐
            │ Redis Server │
            │  (Queues)    │
            └──┬───────┬───┘
               │       │
    Trial jobs │       │ Verify jobs
               ▼       ▼
    ┌──────────────┐  ┌──────────────────────┐
    │ Workers (×N) │  │ Evaluators (×M)      │
    │              │  │       (optional)      │
    │ Execute CRS  │──│ Build variants       │
    │ trials       │  │ Verify POVs          │
    └──────────────┘  └──────────────────────┘
                │              ▲
                └──────────────┘
              Enqueue POVs for verify
```

- **Orchestrator** (`crsbench run`): generates trial matrix and enqueues CRS trial jobs
- **Workers** (`crsbench worker`): execute CRS trials, discover POVs, enqueue verification
- **Evaluators** (`crsbench evaluator`, optional): build variant images and verify POVs

**Queue names (same Redis instance):**
- `crsbench_{experiment}` — CRS trial jobs (workers consume)
- `crsbench_{experiment}_verify` — POV verification jobs (evaluators consume)

## Prerequisites

1. **Docker** installed and running
2. **CRSBench** installed (`uv pip install -e .`)
3. **Valkey** (Redis-compatible server) — start with Docker:

```bash
# Local development (host access, no auth)
python scripts/valkey-helper.py start --bind-host

# Remote workers (password auth, binds 0.0.0.0)
python scripts/valkey-helper.py start --password
scp .env user@worker-machine:/path/to/CRSBench/.env
```

4. **Experiment config** — see [experiment-config-distributed-example.yaml](experiment-config-distributed-example.yaml) for all options.

## Quick Start

### Step 1: Start Valkey

```bash
python scripts/valkey-helper.py start --bind-host
python scripts/valkey-helper.py status
```

### Step 2: Start workers

```bash
# Start a worker with 4 parallel jobs (in a separate terminal)
crsbench worker --experiment-config config.yaml -j 4 --continuous
```

### Step 3: Run experiment

```bash
crsbench run \
  --experiment-config config.yaml \
  --experiment-name my-exp \
  --crses atlantis-c,atlantis-multilang \
  --benchmarks libjpeg-turbo,libxml2
```

The orchestrator enqueues all trial jobs and monitors progress until completion.

### Step 4 (optional): Start evaluator

```bash
crsbench evaluator \
  --experiment-config config.yaml \
  --experiment-name my-exp \
  -j 2
```

Evaluators build variant Docker images and verify POVs from the verify queue. If no evaluator runs, verification jobs queue harmlessly and can be processed later.

### Step 5 (optional): Re-evaluate

```bash
crsbench re-eval -c config.yaml -v
```

Re-runs POV/patch verification on completed trials without re-running CRS. Useful after fixing verification logic or adding new variants.

### Step 6: Generate report

```bash
crsbench report --experiment my-exp
```

## Configuration

Minimal experiment config:

```yaml
experiment: my-exp
trials: 3
mode: delta
max_total_time: 28800
experiment_filestore: /data/experiments
report_filestore: /data/reports

redis_host: localhost

crses:
  - atlantis-c
benchmarks:
  - libjpeg-turbo

resources:
  cores_per_trial: 8
  memory_per_trial: "16G"
  litellm:
    max_concurrent_requests: 50
    cost_budget: 500.0
```

### Worker path overrides

When workers run on machines with different filesystem layouts, add a `worker` section:

```yaml
worker:
  jobs: 4
  continuous: true
  oss_fuzz_path: /opt/oss-fuzz
  benchmarks_root: /data/benchmarks
  experiment_filestore: /mnt/shared/experiments
  report_filestore: /mnt/shared/reports
```

All path overrides are optional. See [experiment-config-distributed-example.yaml](experiment-config-distributed-example.yaml) for the full list.

## Multi-Machine Setup

### Option A: Password Auth (recommended)

**Machine A** (Valkey + Orchestrator):
```bash
python scripts/valkey-helper.py start --password
crsbench run --experiment-config config.yaml --experiment-name my-exp \
  --crses atlantis-c --benchmarks bench1,bench2
```

**Machine B..N** (Workers):
```bash
# Copy .env from Machine A (contains REDIS_PASSWORD)
scp user@machine-a:/path/to/CRSBench/.env /path/to/CRSBench/.env

# Start worker (set REDIS_HOST to Machine A's IP)
crsbench worker --experiment-config config.yaml --redis-host <machine-a-host> -j 4 --continuous
```

### Option B: SSH Tunnels

**Machine A** (Valkey + Orchestrator):
```bash
python scripts/valkey-helper.py start --bind-host
crsbench run --experiment-config config.yaml --experiment-name my-exp \
  --crses atlantis-c --benchmarks bench1,bench2
```

**Machine B..N** (Workers):
```bash
# Tunnel to Machine A's Valkey
ssh -N -L 6379:localhost:6379 user@machine-a &

# Start worker (redis_host=localhost via tunnel)
crsbench worker --experiment-config config.yaml --redis-host localhost -j 4 --continuous
```

For persistent tunnels during long experiments:
```bash
autossh -M 0 -f -N -L 6379:localhost:6379 user@machine-a \
  -o "ServerAliveInterval 30" -o "ServerAliveCountMax 3"
```

## Evaluator

The evaluator builds variant Docker images (vulnerable, allpatched, CPV) and verifies POVs discovered by workers.

```bash
crsbench evaluator \
  --experiment-config config.yaml \
  --experiment-name my-exp \
  --redis-host localhost \
  -j 2
```

| Argument | Description | Default |
|----------|-------------|---------|
| `--experiment-config` | Path to experiment config YAML | Required |
| `--experiment-name` | Experiment identifier (queue name) | Required |
| `--redis-host` | Redis server hostname | `localhost` |
| `-j`, `--jobs` | Number of parallel verify jobs | `1` |
| `--no-cpuset` | Disable CPU affinity | `false` |

Evaluators on different machines can use environment variable overrides:
```bash
export CRSBENCH_EVALUATOR_OSS_FUZZ_PATH=/opt/oss-fuzz
export CRSBENCH_EVALUATOR_BENCHMARKS_ROOT=/data/benchmarks
crsbench evaluator --experiment-config config.yaml --experiment-name my-exp
```

## Re-evaluation

Re-runs POV/patch verification on completed experiment trials without re-running CRS.

```bash
# Basic re-evaluation
crsbench re-eval -c config.yaml

# With verbose output and custom timeout
crsbench re-eval -c config.yaml -v --per-pov-verify-timeout 300

# Force rebuild variants and write to separate output
crsbench re-eval -c config.yaml --force-rebuild --output /tmp/reeval-results
```

| Flag | Description |
|------|-------------|
| `-c`, `--experiment-config` | Path to experiment config YAML (required) |
| `--force-rebuild` | Force rebuild of variant images |
| `--per-pov-verify-timeout` | Timeout per POV verification (seconds) |
| `--output`, `-o` | Output directory (default: write to trial dirs) |
| `--build-workers` | Number of parallel build workers |
| `--verify-workers` | Number of parallel verify workers |
| `-v`, `--verbose` | Enable verbose logging |

## Valkey Cleanup

Clean queues between experiments to avoid stale data:

```bash
# Clean specific experiment
python scripts/valkey-helper.py clean my-old-exp

# Clean all experiments (with confirmation)
python scripts/valkey-helper.py clean-all

# Check queue state
python scripts/valkey-helper.py list-queues
```

Always clean before re-running an experiment with the same name or after an interrupted run. See `services/valkey/` for advanced Valkey management.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Workers not picking up jobs | Queue name mismatch | Verify `--experiment-name` matches between orchestrator and workers |
| "Redis not available" | Valkey not running or wrong host | `python scripts/valkey-helper.py status`; check `redis_host` in config |
| Workers exit immediately | Queue is empty (burst mode) | Use `--continuous` flag to keep workers running |
| Stale jobs from previous run | Queue not cleaned | `python scripts/valkey-helper.py clean <experiment>` |
| Jobs failing silently | Check failed job registry | `valkey-cli SMEMBERS rq:failed:crsbench_<exp>` |

## CLI Reference

| Command | Description |
|---------|-------------|
| `crsbench run` | Enqueue trial jobs and monitor progress |
| `crsbench worker` | Pull and execute trial jobs from queue |
| `crsbench evaluator` | Build variants and verify POVs from queue |
| `crsbench re-eval` | Re-run verification on completed trials |
| `crsbench report` | Generate experiment reports |
| `crsbench verify` | Standalone POV verification |
| `crsbench patch-verify` | Standalone patch verification |
| `crsbench coverage` | Collect code coverage |
| `crsbench dashboard` | Launch web dashboard |

## See Also

- [Experiment Config Example](experiment-config-distributed-example.yaml) — full configuration reference
- [Design: Distributed Job Queue](../design-docs/distributed/distributed-job-queue.md) — job queue architecture
- [Design: Distributed Evaluation](../design-docs/distributed/distributed-evaluation.md) — evaluator architecture
- [Deployment Guide](../design-docs/distributed/deployment-guide.md) — detailed multi-machine setup
- [Environment Setup](environment-setup.md) — environment variables and .env configuration
- [Snapshot System](snapshot-examples.md) — progress monitoring during trials
