# Experiment Workflow

End-to-end guide for running CRSBench experiments.

## Architecture

CRSBench uses a distributed model backed by a Redis-compatible queue (Valkey):

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

**Required processes:**
- **Orchestrator** (`crsbench run`): generates trial matrix and enqueues CRS trial jobs
- **Workers** (`crsbench worker`): execute CRS trials, discover POVs

**Optional processes:**
- **Evaluator** (`crsbench evaluator`): build variant images and verify discovered POVs/patches
- **Remote workers**: scale out to additional machines

**Queue names (same Redis instance):**
- `crsbench_{experiment}` — CRS trial jobs (workers consume)
- `crsbench_{experiment}_build` — variant build jobs (evaluators consume)
- `crsbench_{experiment}_verify` — POV/patch verification jobs (evaluators consume)

## Prerequisites

1. **Docker** installed and running
2. **CRSBench** dependencies installed (`uv sync`)
3. **Valkey** (Redis-compatible server):

```bash
# Local development (host access, no auth)
python scripts/valkey-helper.py start

# Remote workers (password auth, binds 0.0.0.0)
python scripts/valkey-helper.py --password start
```

4. **Experiment config** — YAML file defining CRSes, benchmarks, timeouts, and resources.

## Quick Start (Single Machine)

```bash
# 1. Start Valkey
python scripts/valkey-helper.py start

# 2. Run orchestrator (enqueues jobs and monitors)
crsbench run --experiment-config config.yaml

# 3. Start worker (separate terminal; job count configured in experiment config under worker.jobs)
crsbench worker --experiment-config config.yaml --continuous

# 4. (Optional) Start evaluator for POV verification (separate terminal)
crsbench evaluator --experiment-config config.yaml

# 5. (Optional) Generate report after completion
python scripts/cpv_report.py /path/to/experiment-data --csv
```

The orchestrator and evaluator can be started in any order. The evaluator is optional — without it, POV verification jobs queue harmlessly and can be processed later via `crsbench re-eval`.

## Full Workflow Example (Production)

A realistic example on a 128-core machine running 7 trial jobs with an evaluator:

```bash
# 1. Start Valkey with password auth (for remote workers)
python scripts/valkey-helper.py --password start

# 2. Start evaluator (cores 112-127, 16 cores)
#    --build-jobs 4: up to 4 parallel variant builds
#    --build-cores-per-job 4: 4 cores per build = 16 cores total
#    --verify-jobs 16: up to 16 parallel POV verifications (1 core each)
crsbench evaluator \
    --experiment-config experiment-configs/experiment-config-afc.yaml \
    --build-jobs 4 \
    --build-cores-per-job 4 \
    --verify-jobs 16 \
    --cores 112-127

# 3. Run orchestrator (enqueues jobs, monitors progress)
crsbench run --experiment-config experiment-configs/experiment-config-afc.yaml

# 4. Start worker (cores 0-111; job count configured in experiment config under worker.jobs)
crsbench worker \
    --experiment-config experiment-configs/experiment-config-afc.yaml \
    --cores 0-111

# 5. (After completion) Generate CPV report
python scripts/cpv_report.py /path/to/experiment-data --csv
```

**Core allocation breakdown:**
```
Cores 0-111  (112 cores) → Worker: 7 jobs × 16 cores/trial
Cores 112-127 (16 cores) → Evaluator: 4 build jobs × 4 cores + 16 verify jobs × 1 core
```

## Configuration

### Minimal experiment config

```yaml
experiment: my-exp
trials: 3
mode: delta
max_total_time: 28800
build_timeout: 300
run_timeout: 300
verify_timeout: 300
pov_dedup_strategy: patch-based
experiment_filestore: /data/experiments
report_filestore: /data/reports

redis_host: localhost:6379  # or localhost:6380

crses:
  - atlantis-c
benchmarks:
  - libjpeg-turbo

# No LLM needed for pure fuzzers
skip_litellm: true

resources:
  cores_per_trial: 16
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

## Evaluator

The evaluator builds variant Docker images (vulnerable, allpatched, CPV) and verifies POVs discovered by workers.

```bash
crsbench evaluator \
  --experiment-config config.yaml \
  --build-jobs 4 \
  --build-cores-per-job 4 \
  --verify-jobs 16 \
  --cores 112-127
```

| Argument | Description | Default |
|----------|-------------|---------|
| `--experiment-config` | Path to experiment config YAML | Required |
| `--build-jobs` | Max concurrent build jobs | `1` |
| `--build-cores-per-job` | CPUs per build job | `1` |
| `--verify-jobs` | Max concurrent verify jobs | `build-jobs × build-cores-per-job` |
| `--cores` | CPU cores (count or range, e.g., `112-127`) | All available |
| `--skip-cpus` | CPUs to exclude (e.g., `0-3,8-11`) | None |
| `--no-cpuset` | Disable CPU affinity | `false` |

The evaluator is optional. Without it:
- Workers still run CRS trials and discover POVs
- Verification jobs queue in Redis and can be processed later
- Use `crsbench re-eval` to verify POVs after the fact

## Worker

Workers pull trial jobs from the queue and execute CRS against benchmarks.

```bash
crsbench worker \
  --experiment-config config.yaml \
  --cores 0-111 \
  --continuous
```

Job count is configured in the experiment config YAML under `worker.jobs`.

| Argument | Description | Default |
|----------|-------------|---------|
| `--experiment-config` | Path to experiment config YAML | Required |
| `--cores` | CPU cores (count or range, e.g., `0-111`) | All available |
| `--skip-cpus` | CPUs to exclude | None |
| `--continuous` | Keep running after queue empties | `false` |
| `--worker-name` | Worker name for identification | Hostname |
| `--no-cpuset` | Disable CPU affinity (only with single job) | `false` |

## Multi-Machine Setup

### Option A: Password Auth (recommended)

**Machine A** (Valkey + Orchestrator + Evaluator):
```bash
# Start Valkey with password auth
python scripts/valkey-helper.py --password start

# Start evaluator
crsbench evaluator --experiment-config config.yaml \
    --build-jobs 4 --build-cores-per-job 4 --verify-jobs 16 --cores 112-127

# Run orchestrator
crsbench run --experiment-config config.yaml

# Start local worker
crsbench worker --experiment-config config.yaml --cores 0-111
```

**Machine B..N** (Remote Workers):
```bash
# Copy .env from Machine A (contains CRSBENCH_REDIS_PASSWORD)
scp user@machine-a:/path/to/CRSBench/.env /path/to/CRSBench/.env

# Setup: bundle packages and prepare environment
scripts/orchestrate-workers.sh setup

# Start worker (set CRSBENCH_REDIS_HOST in .env for Machine A)
crsbench worker --experiment-config config.yaml \
    --continuous
```

After all trials complete, collect experiment data back to the orchestrator:
```bash
# From Machine A: collect results from remote workers
scripts/orchestrate-workers.sh collect
```

> **TODO**: Generalize `scripts/orchestrate-workers.sh setup` and `collect` into
> a standalone `crsbench` subcommand or reusable script that works across
> different machine configurations without hard-coded hostnames.

### Option B: SSH Tunnels

**Machine A** (Valkey + Orchestrator):
```bash
python scripts/valkey-helper.py start
crsbench run --experiment-config config.yaml
```

**Machine B..N** (Workers):
```bash
# Tunnel to Machine A's Valkey
ssh -N -L 6379:localhost:6379 user@machine-a &

# Start worker (set CRSBENCH_REDIS_HOST=localhost:6379 via tunnel)
crsbench worker --experiment-config config.yaml --continuous
```

For persistent tunnels during long experiments:
```bash
autossh -M 0 -f -N -L 6379:localhost:6379 user@machine-a \
  -o "ServerAliveInterval 30" -o "ServerAliveCountMax 3"
```

## Re-evaluation

Re-runs POV/patch verification on completed experiment trials without re-running CRS. Useful when:
- Evaluator wasn't running during the experiment
- Verification logic was updated
- Need to re-collect crash logs

```bash
# Basic re-evaluation with verbose output
crsbench re-eval -c config.yaml -v

# With custom timeout and forced rebuild
crsbench re-eval -c config.yaml --force-rebuild --per-pov-verify-timeout 300

# Write to separate output directory
crsbench re-eval -c config.yaml --output /tmp/reeval-results
```

Re-eval preserves `metadata.json` and relative POV discovery times, but re-runs verification and collects crash logs.

| Flag | Description |
|------|-------------|
| `-c`, `--experiment-config` | Path to experiment config YAML (required) |
| `--force-rebuild` | Force rebuild of variant images |
| `--per-pov-verify-timeout` | Timeout per POV verification (seconds) |
| `--output`, `-o` | Output directory (default: write to trial dirs) |
| `--build-workers` | Number of parallel build workers |
| `--verify-workers` | Number of parallel verify workers |
| `-v`, `--verbose` | Enable verbose logging |

## Reporting

After experiments complete, generate CPV detection reports:

```bash
# Generate CSV report
python scripts/cpv_report.py /path/to/experiment-data --csv

# Generate report with benchmark metadata
python scripts/cpv_report.py /path/to/experiment-data --benchmarks-dir benchmarks/
```

HTML/JSON reports are auto-generated by the orchestrator at completion and saved to the `report_filestore` directory.

## Valkey Management

```bash
# Check status
python scripts/valkey-helper.py status

# Clean specific experiment queues
python scripts/valkey-helper.py clean my-old-exp

# Clean all data (with confirmation)
python scripts/valkey-helper.py clean-all

# Check queue state
python scripts/valkey-helper.py list-queues

# View queue details
python scripts/valkey-helper.py queue-info my-exp

# View statistics
python scripts/valkey-helper.py stats
```

Always clean queues before re-running an experiment with the same name or after an interrupted run.

## CI Smoke Secrets

For GitHub smoke workflows (`ci.yml` and `smoke-crs-regression.yml`) using bug-fixing CRS in passthrough mode, configure these repository secrets:

- `CRSBENCH_LLM_UPSTREAM_BASE_URL`
- `CRSBENCH_LLM_API_KEY` (or `CRSBENCH_LLM_MASTER_KEY`)

Smoke bug-fixing runs use `llm_tracking_enabled: false`, so `CRSBENCH_LLM_MASTER_KEY` is optional for smoke.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Workers not picking up jobs | Queue name mismatch | Verify `experiment` in config is identical on orchestrator and workers |
| "Redis not available" | Valkey not running or wrong host | `python scripts/valkey-helper.py status`; check `redis_host` in config |
| Workers exit immediately | Queue is empty (burst mode) | Use `--continuous` flag to keep workers running |
| Stale jobs from previous run | Queue not cleaned | `python scripts/valkey-helper.py clean <experiment>` |
| `CRSBENCH_LLM_UPSTREAM_BASE_URL not set` | LiteLLM not needed but `litellm_mode` still active | Set `skip_litellm: true` in experiment config |

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
- [Design: Distributed Job Queue](./design/distributed/distributed-job-queue.md) — job queue architecture
- [Design: Distributed Evaluation](./design/distributed/distributed-evaluation.md) — evaluator architecture
- [Environment Setup](environment-setup.md) — environment variables and .env configuration
- [Snapshot System](snapshot-examples.md) — progress monitoring during trials

## Upstream OSS-CRS References

- [oss-crs/README.md](../oss-crs/README.md) — lifecycle and command overview
- [oss-crs/docs/design/parallel.md](../oss-crs/docs/design/parallel.md) — build/run IDs and artifact path model
- [oss-crs/docs/config/crs-compose.md](../oss-crs/docs/config/crs-compose.md) — compose configuration fields
