# Distributed Experiments

End-to-end guide for running CRSBench experiments.

## Architecture

CRSBench uses a distributed model backed by a Redis-compatible queue (Valkey).
Commands below use the repo-local invocation style (`uv run ...`) so they are
directly runnable from a fresh clone.

```
┌─────────────────────────────────────────────────────────────┐
│                   CRSBench Orchestrator                     │
│                  (uv run crsbench run)                             │
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
- **Orchestrator** (`uv run crsbench run`): generates trial matrix and enqueues CRS trial jobs
- **Workers** (`uv run crsbench worker`): execute CRS trials, discover POVs

**Optional processes:**
- **Evaluator** (`uv run crsbench evaluator`): build variant images and verify discovered POVs/patches
- **Remote workers**: scale out to additional machines

**Queue names (same Redis instance):**
- Default queue model (`CRSBENCH_QUEUE_MODEL=flat`):
  - `crsbench_trial` — CRS trial jobs (workers consume)
  - `crsbench_build` — variant build jobs (evaluators consume)
  - `crsbench_verify` — POV/patch verification jobs (evaluators consume)
- Optional legacy model (`CRSBENCH_QUEUE_MODEL=per-experiment`):
  - `crsbench_{experiment}`
  - `crsbench_{experiment}_build`
  - `crsbench_{experiment}_verify`

For canonical queue-model behavior and configless details, see
[`docs/design/distributed/configless-runtime.md`](../../design/distributed/configless-runtime.md).

## Prerequisites

1. **Docker** installed and running
2. **CRSBench** dependencies installed (`uv sync`)
3. **Valkey** (Redis-compatible server):

```bash
# Local development (host access, no auth)
uv run python scripts/valkey-helper.py start

# Remote workers (password auth, binds 0.0.0.0)
uv run python scripts/valkey-helper.py --password start
```

4. **Experiment config** — YAML file defining CRSes, benchmarks, timeouts, and resources.

## Quick Start (Single Machine)

```bash
# 1. Start Valkey
uv run python scripts/valkey-helper.py start

# 2. Start worker (separate terminal; required for trial progress)
uv run crsbench worker --experiment-config config.yaml

# 3. Run orchestrator (enqueues jobs and monitors)
uv run crsbench run --experiment-config config.yaml

# 4. (Optional) Start evaluator for build/verify queue processing
uv run crsbench evaluator --experiment-config config.yaml --jobs 4 --cores-per-job 4

# 5. (Optional) Generate report after completion
uv run python scripts/cpv_report.py /path/to/experiment-data --csv
```

The worker should be running before or at the same time as the orchestrator so
trial jobs can start immediately. The evaluator is optional — without it,
build/verify work queues harmlessly and can be processed later via
`crsbench re-eval` or a later evaluator run.

## Cloud Worker Fleets

Managed cloud config uses a provider-neutral `cloud.*` layout. Today the only
implemented managed backend is GCE, so declare provider-native details in
`cloud.providers.gce`, then place workers with `cloud.workers.placements`
instead of relying on host maps or ad hoc SSH setup scripts.

```yaml
cloud:
  providers:
    gce:
      project: example-project
      ssh_via_iap: true
      profile_defaults:
        machine_type: n2d-standard-16
        boot_disk_size_gb: 200
        image: projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64
        service_account_email: crsbench-worker@example-project.iam.gserviceaccount.com
        owner_label: team-crs
        readiness_timeout_sec: 900
        env:
          CRSBENCH_LLM_UPSTREAM_BASE_URL: os.environ/LITELLM_BASE_URL
      instance_profiles:
        gce-worker-n2d: {}
  orchestrator:
    zone: us-east5-b
    instance_profile: gce-worker-n2d
  workers:
    defaults:
      instance_profile: gce-worker-n2d
      count: 1
    placements:
      - zone: us-east5-b
        count: 3
        env:
          CRSBENCH_LLM_MASTER_KEY: os.environ/LITELLM1_MASTER_KEY
      - zone: us-east1-b
        env:
          CRSBENCH_LLM_MASTER_KEY: os.environ/LITELLM2_MASTER_KEY
```

Phase 1 contract notes:
- the top-level cloud shape is provider-neutral; today that means `cloud.providers.gce` plus `cloud.workers.defaults` / `cloud.workers.placements`
- Managed GCE placement supports both explicit zonal declarations and regional placement via `region` / `regions`.
- When an effective `region` or ordered `regions` list is present, CRSBench uses GCE regional bulk insert with `ANY_SINGLE_ZONE`.
- Optional `zones` act as an allowlist inside the effective region set rather than an ordered retry list.
- `fallback: true` retries later declared regions or zones only for recognized capacity failures; `fallback: false` fails that logical placement and rolls back the launch.
- Access is OS Login-compatible SSH only; keep host verification enabled.
- Use a dedicated worker service account rather than default project
  credentials.
- `ssh_via_iap: true` is the preferred pattern when workers are not exposed on
  public SSH.
- Cloud worker readiness is an explicit control-plane concern and is distinct
  from generic Redis worker visibility.
- Bootstrapped cloud workers stay pinned to the declaring experiment instead
  of joining the shared configless worker pool.
- In pre-provisioned cloud mode, `crsbench run` waits for the declared fleet
  to exist, then enqueues jobs even if some workers or evaluators are still
  booting. Explicit readiness remains a health/reporting signal rather than a
  hard enqueue gate.
- A VM in GCE `RUNNING` state is still non-ready until CRSBench records
  `ready`.
- Bootstrap failures are surfaced through per-instance startup evidence, so
  normal diagnosis should not require interactive SSH.
- Failed cloud bring-up tears down the requested fleet before control returns
  to the operator.
- First-class `cloud.env` / profile `env` / placement `env` maps are the
  supported way to shard upstream credentials or URLs across cloud worker
  groups.
- For the full managed-cloud lifecycle on the current GCE backend, including
  read-only `cloud preflight`, `cloud launch`, `cloud monitor`, `cloud collect`,
  and `cloud teardown`, use [GCE Cloud Orchestration](./gce-cloud-orchestration.md).

## Queue Behavior and Cleanup

Use experiment-scoped queue cleanup (safe in flat shared-queue mode):

```bash
uv run crsbench queue clean --experiment <experiment-name> --yes
```

Optional scoped cleanup:

```bash
uv run crsbench queue clean --experiment <experiment-name> --queues trial,verify --yes
```

`crsbench run` queue behavior:
- TTY: prompts for `fresh` / `continue` / `quit` when existing jobs are found.
- Non-TTY (CI): defaults to scoped `continue` (no prompt).
- `continue`: skips existing jobs and handles orphaned started jobs.
- Failed jobs are retried only with explicit opt-in:

```bash
uv run crsbench run --experiment-config config.yaml --queue-mode continue --retry-failed
```

## Benchmark CI Flag Semantics

For modular benchmark-ci commands (`crsbench benchmark ci all|build|pov|patch|coverage`):

- `--exit-on-error` is currently a compatibility flag (accepted, no-op).
- With `--distributed`, set concurrency on evaluator processes (`crsbench evaluator --ci --jobs ... --cores-per-job ...`, or split `--build-*` / `--verify-*` only for asymmetric tuning).
- In `crsbench evaluator --ci`, `--worker-name` defaults to `ci-evaluator` when omitted.

## Full Workflow Example (Production Example)

This is one illustrative 128-core topology running 7 trial jobs with an
evaluator. It is not the only valid sizing layout.

```bash
# 1. Start Valkey with password auth (for remote workers)
uv run python scripts/valkey-helper.py --password start

# 2. Start worker (cores 0-111; explicit concurrency shown)
uv run crsbench worker \
    --jobs 7 \
    --cores-per-job 16 \
    --cpuset 0-111

# 3. (Optional) Start evaluator (cores 112-127; explicit concurrency shown)
uv run crsbench evaluator \
    --jobs 4 \
    --cores-per-job 4 \
    --cpuset 112-127

# 4. Run orchestrator (enqueues jobs, monitors progress)
uv run crsbench run --experiment-config experiment-configs/afc-final-bugfinding/atlantis-multilang-given_fuzzer-default-full-given-fuzzer-run.yaml

# 5. (After completion) Generate CPV report
uv run python scripts/cpv_report.py /path/to/experiment-data --csv
```

**Core allocation breakdown:**
```
Cores 0-111  (112 cores) → Worker: 7 jobs × 16 cores/trial
Cores 112-127 (16 cores) → Evaluator: 4 jobs × 4 cores/job in this example
```

## Configuration

### Minimal experiment config

```yaml
experiment:
  name: my-exp
  task: bugfixing
  mode: delta
  benchmark_suite: afc-final
  sanitizers: [address, undefined]

runtime:
  trials: 3
  max_total_time: 28800
  build_timeout: 3600
  run_timeout: 14400
  verify_timeout: 7200
  redis_host: localhost:6379  # or localhost:6380
  litellm:
    skip: true
  # Optional LiteLLM runtime contract
  # litellm:
  #   mode: external
  #   tracking_enabled: true
  inputs:
    pov:
      max_variants_per_cpv: 1

storage:
  experiment_filestore: /data/experiments
  report_filestore: /data/reports

# Optional top-level verification/reporting behavior:
# pov_dedup_strategy: patch-based

crs_compose:
  crs-codex:
    num_cores: 16
# If you use benchmark_suite, omit benchmarks.
# Alternatively:
# experiment:
#   benchmarks:
#     - libjpeg-turbo

resources:
  cores_per_trial: 16
  memory_per_trial: "16G"

evaluator:
  jobs: 4
  cores_per_job: 4
  # Optional advanced split overrides:
  # build_jobs: 4
  # build_cores_per_job: 4
  # verify_jobs: 16
  # verify_cores_per_job: 1

cloud:
  providers:
    gce:
      project: example-project
      ssh_via_iap: true
      profile_defaults:
        machine_type: n2d-standard-16
        boot_disk_size_gb: 200
        image: projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64
        service_account_email: crsbench-worker@example-project.iam.gserviceaccount.com
        owner_label: team-crs
        readiness_timeout_sec: 900
      instance_profiles:
        gce-worker-n2d: {}
  orchestrator:
    zone: us-east5-b
    instance_profile: gce-worker-n2d
  workers:
    defaults:
      instance_profile: gce-worker-n2d
      count: 1
    placements:
      - zone: us-east5-b
        count: 4
```

### Config File Naming

Experiment config filenames are a repository convention for readability only. CRSBench does not enforce any filename schema; only YAML content is validated.

### Inputs Contract

`runtime.inputs` is the canonical input contract. Define POV/SARIF/seed/diff
explicitly in config.

Practical input combinations:

```yaml
# 1) POV-only (bug-fixing)
runtime:
  inputs:
    pov:
      max_variants_per_cpv: 1
```

```yaml
# 2) SARIF level 1 only
runtime:
  inputs:
    sarif:
      level: 1
```

```yaml
# 3) Seed corpus only
runtime:
  inputs:
    seed:
      max_time: 3600
```

```yaml
# 4) Combined inputs (POV + SARIF + seed corpus)
runtime:
  inputs:
    pov:
      max_variants_per_cpv: 3
    sarif:
      level: 1
    seed:
      max_time: 3600
```

Legacy top-level input knobs are compatibility-only; new configs should use
`runtime.inputs.*`.

### Worker Machine Overrides

When workers run on machines with different filesystem layouts, keep primary roots in top-level `storage`, then add machine-local overrides under `worker.storage`:

```yaml
worker:
  jobs: 4
  continuous: true
  benchmarks_root: /mnt/shared/benchmarks
  storage:
    experiment_filestore: /mnt/shared/experiments
    report_filestore: /mnt/shared/reports
    # optional:
    # results_filestore: /mnt/shared/finished
```

CPU placement is operator-side in distributed mode (CLI on worker/evaluator):
`--cpuset` and `--skip-cpuset`.

See [experiment-config-distributed-example.yaml](../../experiment-config-distributed-example.yaml) for the concise contract.

If worker machines do not mount benchmarks at the same path as the orchestrator,
set `worker.benchmarks_root` to the machine-local path used on that worker.
Use this only for heterogeneous filesystem layouts; keep shared-storage paths
uniform when possible.

## Centralized LiteLLM / Proxy Mode

When LiteLLM runs centrally and experiment machines proxy through it:

- central LiteLLM keeps the provider keys
- trial hosts set only the upstream LiteLLM endpoint and upstream key
- worker and evaluator hosts do not need provider API keys in that model

If your workflow depends on the upstream-model synchronization helper:

```bash
uv run python scripts/sync-upstream-models.py --list-only
uv run python scripts/sync-upstream-models.py
```

## Evaluator

The evaluator builds variant Docker images (vulnerable, allpatched, CPV) and verifies POVs discovered by workers.

```bash
uv run crsbench evaluator \
  --experiment-config config.yaml \
  --jobs 4 \
  --cores-per-job 4 \
  --cpuset 112-127
```

| Argument | Description | Default |
|----------|-------------|---------|
| `--experiment-config` | Path to experiment config YAML | Optional (configless discovery when omitted) |
| `--jobs` | Default concurrent evaluator jobs used for both build and verify when split overrides are not set | From evaluator config/default policy |
| `--cores-per-job` | Default CPUs per evaluator job used for both build and verify when split overrides are not set | From evaluator config/default policy |
| `--build-jobs` | Advanced split override: max concurrent build jobs | From evaluator config/default policy |
| `--build-cores-per-job` | Advanced split override: CPUs per build job | From evaluator config/default policy |
| `--verify-jobs` | Advanced split override: max concurrent verify jobs | From config/default policy |
| `--verify-cores-per-job` | Advanced split override: CPUs per verify job | From evaluator config/default policy |
| `--cpuset` | CPU cores (count or range, e.g., `112-127`) | CPU affinity disabled unless set |
| `--skip-cpuset` | CPUs to exclude (e.g., `0-3,8-11`) | None |
| `--cpu-tag` | Run only jobs matching this capability tag | None |
| `--idle-timeout` | Exit after N idle seconds once queues drain | `0` |
| `--worker-name` | Evaluator instance name in logs/metadata | Mode-specific auto-generated name (`configless-evaluator` / `ci-evaluator` / `evaluator-<experiment>`) |
| `--ci` | Use CI queue aliases (`crsbench_ci_*`) | Off |

Config v2 evaluator defaults use unified knobs:
- `evaluator.jobs` (default: unset; runtime falls back to one build job when no
  explicit build concurrency is configured)
- `evaluator.cores_per_job` (default: unset; runtime derives effective CPU width
  from the current visible CPU envelope / cpuset policy)

Optional split overrides are only for asymmetric tuning:
- `evaluator.build_jobs`, `evaluator.build_cores_per_job`
- `evaluator.verify_jobs`, `evaluator.verify_cores_per_job`

The evaluator is optional. Without it:
- Workers still run CRS trials and discover POVs
- Verification jobs queue in Redis and can be processed later
- Use `crsbench re-eval` to verify POVs after the fact

Mode note:
- config-pinned evaluator CLI mode normally performs a startup pre-build
  enqueue phase
- configless evaluator mode skips startup pre-build enqueue and consumes build
  work lazily; async POV verification enqueues benchmark-local build jobs on
  first POV discovery and verify jobs wait on those build dependencies

`runtime.verify_timeout` is the async verification drain budget. When POVs are
still queued at trial shutdown, CRSBench waits up to that budget for the
remaining build and verify work to finish before marking the outstanding POVs
as verification errors.

## Worker

Workers pull trial jobs from the queue and execute CRS against benchmarks.

```bash
uv run crsbench worker \
  --experiment-config config.yaml \
  --cpuset 0-111
```

| Argument | Description | Default |
|----------|-------------|---------|
| `--experiment-config` | Path to experiment config YAML | Optional (configless discovery when omitted) |
| `--jobs` | Max concurrent trial jobs in this worker process | From worker config/default policy |
| `--cores-per-job` | CPUs per trial job | From worker config/default policy |
| `--cpuset` | CPU cores (count or range, e.g., `0-111`) | CPU affinity disabled unless set |
| `--skip-cpuset` | CPUs to exclude | None |
| `--cpu-tag` | Run only jobs matching this capability tag | None |
| `--continuous` | Keep running after queue empties | Enabled by default |
| `--no-continuous` | Exit after the current backlog drains | Off |
| `--worker-name` | Worker name for identification | Hostname |

## Multi-Machine Setup

The manual SSH/scp workflows below are the legacy operator-managed path. They
remain useful for existing non-cloud deployments, but they are not the managed
cloud worker contract. For GCE-backed fleets, declare workers in
`cloud.providers.gce` plus `cloud.workers.placements` instead of encoding
hostnames into scripts.

### Option A: Password Auth (recommended)

**Machine A** (Valkey + Orchestrator + Evaluator):
```bash
# Start Valkey with password auth
uv run python scripts/valkey-helper.py --password start

# Start evaluator
uv run crsbench evaluator --experiment-config config.yaml \
    --cpuset 112-127

# Run orchestrator
uv run crsbench run --experiment-config config.yaml

# Start local worker
uv run crsbench worker --experiment-config config.yaml --cpuset 0-111
```

**Machine B..N** (Remote Workers):
```bash
# Copy .env from Machine A (contains CRSBENCH_REDIS_PASSWORD)
scp user@machine-a:/path/to/CRSBench/.env /path/to/CRSBench/.env

# Setup: bundle packages and prepare environment
scripts/orchestrate-workers.sh setup

# Start worker (set CRSBENCH_REDIS_HOST in .env for Machine A)
uv run crsbench worker --experiment-config config.yaml
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
uv run python scripts/valkey-helper.py start
uv run crsbench run --experiment-config config.yaml
```

**Machine B..N** (Workers):
```bash
# Tunnel to Machine A's Valkey
ssh -N -L 6379:localhost:6379 user@machine-a &

# Start worker (set CRSBENCH_REDIS_HOST=localhost:6379 via tunnel)
uv run crsbench worker --experiment-config config.yaml
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

Patch verification test execution context:
- CRSBench evaluator runs unit tests inside the project builder image (`test.sh`/`run_tests.sh`) after patch build.
- Test script path is resolved from `oss-fuzz/projects/<variant>/test.sh` (fallback: `run_tests.sh`) in evaluator workspace.
- Unit test containers use Docker's default network mode by default so benchmark scripts that install runtime test deps (for example `apt`/`pip`) can execute.
- You can override network mode for helper/direct Docker runs with `OSS_FUZZ_DOCKER_NETWORK` (for example `none`).
- Before running tests, evaluator resolves the effective image `WORKDIR` (image inspect, Dockerfile fallback) and syncs patched source into that directory.
- Internal test-container mounts are namespaced as `CRSBENCH_*` paths (for example `/CRSBENCH_PROJ_PATH`, `/CRSBENCH_PATCHED_SRC`).
- This is separate from OSS-CRS builder sidecar `run-test`, which resolves `/OSS_CRS_PROJ_PATH/test.sh` inside the runtime snapshot image.

```bash
# Basic re-evaluation with verbose output
uv run crsbench re-eval -c config.yaml -v

# With custom timeout and forced rebuild
uv run crsbench re-eval -c config.yaml --force-rebuild --per-pov-verify-timeout 300

# Write to separate output directory
uv run crsbench re-eval -c config.yaml --output /tmp/reeval-results
```

Re-eval preserves `metadata.json` and relative POV discovery times, but re-runs verification and collects crash logs.
For patch-generation trials, `patch_verify_variants` in the experiment config
controls whether patch verification checks all `pov_*` variants (`true`) or
single-POV mode (`false`, default).

Bug-finding re-eval duplicate handling:
- Re-eval verifies at most one file per unique POV content hash when reading from trial `output/povs`.
- Selection is deterministic (filename order), so repeated runs choose the same representative file.
- This behavior aligns local re-eval with distributed async single-POV verification semantics.

| Flag | Description |
|------|-------------|
| `-c`, `--experiment-config` | Path to experiment config YAML (required) |
| `--force-rebuild` | Force rebuild of variant images |
| `--per-pov-verify-timeout` | Timeout per POV verification (seconds) |
| `--output`, `-o` | Output directory (default: write to trial dirs) |
| `--jobs` | Number of parallel verification jobs |
| `--cores-per-job` | CPUs per verification job |
| `-v`, `--verbose` | Enable verbose logging |

## Reporting

After experiments complete, generate CPV detection reports:

```bash
# Generate CSV report
uv run python scripts/cpv_report.py /path/to/experiment-data --csv

# Generate report with benchmark metadata
uv run python scripts/cpv_report.py /path/to/experiment-data --benchmarks-dir benchmarks/
```

HTML/JSON reports are auto-generated by the orchestrator at completion and saved to the `report_filestore` directory.

## Valkey Management

```bash
# Check status
uv run python scripts/valkey-helper.py status

# Clean specific experiment queues
uv run python scripts/valkey-helper.py clean my-old-exp

# Clean all data (with confirmation)
uv run python scripts/valkey-helper.py clean-all

# Check queue state
uv run python scripts/valkey-helper.py list-queues

# View queue details
uv run python scripts/valkey-helper.py queue-info my-exp

# View statistics
uv run python scripts/valkey-helper.py stats
```

Always clean queues before re-running an experiment with the same name or after an interrupted run.

## CI Smoke Secrets

For GitHub smoke workflow (`ci.yml`) using bug-fixing CRS in external mode, configure these repository secrets:

- `CRSBENCH_LLM_UPSTREAM_BASE_URL`
- `CRSBENCH_LLM_UPSTREAM_MASTER_KEY`

If your suite sets `runtime.litellm.tracking_enabled: false`, `CRSBENCH_LLM_UPSTREAM_API_KEY` can be enough for basic runtime requests.

Smoke bug-fixing suites currently run with LiteLLM tracking enabled in the sanity bug-fixing smoke config (`experiment-configs/sanity-bugfixing/...`), so provide the upstream key expected by your LiteLLM deployment.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Workers not picking up jobs | Queue name mismatch | Verify `experiment` in config is identical on orchestrator and workers |
| "Redis not available" | Valkey not running or wrong host | `uv run python scripts/valkey-helper.py status`; check `redis_host` in config |
| Workers exit immediately | Worker was started with `--no-continuous` and the queue drained | Omit `--no-continuous` for the default continuous mode |
| Stale jobs from previous run | Queue not cleaned | `uv run python scripts/valkey-helper.py clean <experiment>` |
| `CRSBENCH_LLM_UPSTREAM_BASE_URL not set` | LiteLLM env contract is incomplete for this trial | Set `skip_litellm: true` when LLM is not needed, or provide required `CRSBENCH_LLM_*` vars |

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
| `crsbench coverage` | Collect code coverage (experimental) |
| `crsbench dashboard` | Launch web dashboard |

## See Also

- [Experiment Config Example](../../experiment-config-distributed-example.yaml) — full configuration reference
- [Design: Distributed Job Queue](../../design/distributed/distributed-job-queue.md) — job queue architecture
- [Design: Distributed Evaluation](../../design/distributed/distributed-evaluation.md) — evaluator architecture
- [Configuration](../../getting-started/configuration.md) — environment variables and .env configuration
- [Snapshots](../../reference/snapshots.md) — progress monitoring during trials

## Upstream OSS-CRS References

- [oss-crs/README.md](../../../oss-crs/README.md) — lifecycle and command overview
- [oss-crs/docs/design/parallel.md](../../../oss-crs/docs/design/parallel.md) — build/run IDs and artifact path model
- [oss-crs/docs/config/crs-compose.md](../../../oss-crs/docs/config/crs-compose.md) — compose configuration fields
