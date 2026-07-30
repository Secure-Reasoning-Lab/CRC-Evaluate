# Benchmark CI

End-to-end testing for CRSBench benchmarks: format validation, build, POV
verification, patch verification, unit tests, and coverage.

In distributed mode, `crsbench evaluator --ci` workers execute CI build jobs and
verify/test jobs (POV, patch, unit tests) from Redis queues.

## What Gets Tested

| Stage | What | Docker? |
|-------|------|---------|
| **Format** | meta.yaml, project.yaml, Dockerfile, POV files, patches | No |
| **Build** | Build vulnerable, allpatched, and per-CPV variants | Yes |
| **POV** | POVs trigger correct CPVs on vulnerable variant | Yes |
| **Patch** | Ground-truth patches fix vulnerabilities | Yes |
| **Unit Test (FULL)** | test.sh passes on patched variant | Yes |
| **Coverage** | Coverage collection on vulnerable variant (optional) | Yes |

## Quick Start

### Prerequisites

```bash
# Install dependencies
uv sync

# Docker must be running (for build/verify stages)
docker info
```

### Run All Checks on a Single Benchmark

```bash
crsbench benchmark ci all benchmarks/afc-curl-delta-01
```

### Run All Checks on All Benchmarks

```bash
crsbench benchmark ci all --all
```

## CLI Reference

### Subcommands

```bash
crsbench benchmark ci format    # Format validation only (no Docker)
crsbench benchmark ci build     # Build variants only (no verification)
crsbench benchmark ci pov       # Build + POV verification
crsbench benchmark ci patch     # Build + patch verification + unit tests
crsbench benchmark ci coverage  # Build + coverage collection (experimental)
crsbench benchmark ci capabilities  # Show benchmark capabilities (may inspect/pull Docker images)
crsbench benchmark ci all       # All of the above (coverage with --inc-coverage)
crsbench benchmark ci parse     # Parse results from a previous run
crsbench benchmark ci retry     # Retry failed benchmarks from a previous run
crsbench benchmark ci storage   # Show storage usage per benchmark (no Docker)
```

### Benchmark Selection

Benchmark-selection options are supported by:
`format`, `build`, `pov`, `patch`, `coverage` (experimental), `all`, `capabilities`, and `storage`.

```bash
# Single benchmark (positional)
crsbench benchmark ci all benchmarks/afc-curl-delta-01

# Multiple benchmarks (space-separated)
crsbench benchmark ci all -b afc-curl-delta-01 afc-curl-delta-02 afc-tika-delta-01

# Filter by glob pattern
crsbench benchmark ci all --filter "afc-curl-*"
crsbench benchmark ci all --filter "atlanta-*"

# All benchmarks
crsbench benchmark ci all --all

# From a suite file
crsbench benchmark ci all -s smoke-test-bug-finding
```

### Common Options

```bash
--source {pkgs,main_repo}   # Source mode (default: pkgs = bundled tarballs)
--mode {snapshot,full}       # Build mode (default: snapshot)
--force-rebuild              # Rebuild even if cached (default OFF; cache reuse enabled)
--exit-on-error              # Compatibility flag (currently no-op in modular benchmark-ci subcommands)
--output-dir DIR             # Save per-job logs and artifacts
--output FILE                # Save summary JSON
--no-color                   # Disable colored output
--max-povs-per-cpv N         # Limit POVs verified per CPV
```

Build/mode options (`--source`, `--mode`, `--force-rebuild`, `--max-povs-per-cpv`)
apply to build-oriented execution commands (`build`, `pov`, `patch`, `coverage`,
`all`, and `retry` where applicable), not `capabilities` or `storage`.
`retry` defaults to `--mode full` unless overridden.

## Local Execution

By default, DAG-based subcommands (`build`, `pov`, `patch`, `coverage`, `all`,
`retry`) run locally in a single process. Jobs are topologically sorted by
dependencies and executed **sequentially**. `format` supports `--parallel`.

```bash
# Run all checks on all benchmarks (sequential)
crsbench benchmark ci all --all --output-dir ci-results/

# Format check only (fast, no Docker)
crsbench benchmark ci format --all

# Build only (no verification)
crsbench benchmark ci build --all

# POV verification only
crsbench benchmark ci pov --filter "afc-curl-*"

# Patch + unit tests
crsbench benchmark ci patch --filter "afc-zookeeper-*"

# Include coverage (expensive)
crsbench benchmark ci all --all --inc-coverage --output-dir ci-results/

# Force full-mode behavior (no snapshot/inc-build reuse)
crsbench benchmark ci all --all --mode full
```

**Note**: Distributed parallelism is controlled by evaluator worker flags
(`crsbench evaluator --ci --jobs ... --cores-per-job ...`, with split
`--build-*` / `--verify-*` overrides only for asymmetric tuning).

### Execution Flow (Local)

```
crsbench benchmark ci all --all
  |
  |-- Phase 1: Format validation (fast, no Docker)
  |     Runs validate_benchmark() on each benchmark
  |
  |-- Phase 2: Build DAG + Execute
  |     1. Build DAG with per-variant jobs and dependencies
  |     2. Topological sort (Kahn's algorithm)
  |     3. Execute each job sequentially
  |
  |-- Phase 3: Aggregate results + print table
```

## Distributed Execution (Redis)

For parallel execution across multiple CPU cores or machines, use Redis RQ.

### Recommended CI Workflow (Multi-Machine)

Recommended default topology for reliability and deterministic scheduling:

- **1 submitter**: `crsbench benchmark ci ... --distributed`
- **1 evaluator machine**: `crsbench evaluator --ci ...` (CI build + verify queues)
- **1 Redis/Valkey** shared by all machines
- **Shared output path** for logs/artifacts

Execution order:

```bash
# 1) Start Redis/Valkey
python scripts/valkey-helper.py --password start

# 2) Start evaluator (single machine, recommended)
crsbench evaluator --ci \
  --jobs 8 --cores-per-job 16 \
  --idle-timeout 0

# 3) Submit CI jobs from submitter machine
crsbench benchmark ci all --all \
  --distributed \
  --mode snapshot \
  --output-dir ./ci-output
```

Notes:
- Use **one evaluator** by default. Multiple evaluators are possible, but require
  strict queue partitioning to avoid coordination complexity.
- `--mode snapshot` assumes images are prepared/cached; use `--mode full` for
  rebuild-oriented validation.

### Architecture

```
Machine A (CI submitter + Redis/Valkey)
┌──────────────────────────────────────────────┐
│ crsbench benchmark ci ... --distributed      │
│ (builds DAG, enqueues CI jobs, waits result) │
└──────────────────────┬───────────────────────┘
                       │ enqueue/poll
                       v
                ┌───────────────┐
                │ Redis / RQ    │
                │ crsbench_ci_* │
                └───────┬───────┘
                        │ dequeue
                        v
Machine B (single evaluator, recommended)
┌──────────────────────────────────────────────┐
│ crsbench evaluator --ci                      │
│ - consumes build queue + verify queue        │
│ - executes build / verify / patch test jobs  │
└──────────────────────┬───────────────────────┘
                       │ writes logs/artifacts
                       v
             output-dir/<benchmark>/{build,verify}/...
```

The submitter builds the DAG, serializes jobs, enqueues them to Redis, then
polls until all complete. Evaluators dequeue and execute jobs.

### Step-by-Step Setup

#### 1. Start Valkey (Redis)

CRSBench uses Valkey (Redis-compatible) for job queues. Use the helper script:

```bash
# Local development (localhost:6379, no auth)
python scripts/valkey-helper.py start

# Multi-machine setup (0.0.0.0:6379, password auth)
python scripts/valkey-helper.py --password start
# Copy .env to evaluator machine(s): scp .env user@evaluator:/path/to/CRC-Evaluate/.env

# Check status
python scripts/valkey-helper.py status

# Queue management
python scripts/valkey-helper.py list-queues
python scripts/valkey-helper.py clean my-experiment
python scripts/valkey-helper.py clean-all
```

For manual Redis setup: `docker run -d --name redis -p 6379:6379 redis:7`

When configuring CRSBench, set `CRSBENCH_REDIS_HOST` as `host` or `host:port`
(for example `localhost:6379` or `redis.internal:6380`).

#### 2. Start Evaluator

For CI, evaluator runs a dual-queue supervisor that processes build and
verify/test jobs concurrently, with fair scheduling across runnable work.

```bash
# Recommended default: single evaluator machine for CI.
uv run crsbench evaluator --ci --jobs 8 --cores-per-job 16
```

**Evaluator options:**

| Flag | Description | Default |
|------|-------------|---------|
| `--ci` | Use CI queue mode (build + verify) | Required |
| `--jobs N` | Default concurrent evaluator jobs used for both build and verify when split overrides are not set | 1 |
| `--cores-per-job M` | Default CPUs per evaluator job used for both build and verify when split overrides are not set | 4 |
| `--build-jobs N` | Advanced split override: max concurrent build jobs | from `--jobs` |
| `--build-cores-per-job M` | Advanced split override: CPUs per build job | from `--cores-per-job` |
| `--verify-jobs K` | Advanced split override: max concurrent verify/test jobs | from `--jobs` or derived policy |
| `--verify-cores-per-job M` | Advanced split override: CPUs per verify/test job | from `--cores-per-job` |
| `--cpuset CPUSET` | CPU count or cpuset range (e.g., `0-63`) | affinity disabled unless set |
| `--skip-cpuset CPUSET` | CPUs to exclude (e.g., `0-3`) | none |
| `--idle-timeout N` | Exit after N idle seconds (0 = run indefinitely) | 0 |
| `--worker-name NAME` | Identifier for this evaluator process | `ci-evaluator` |

#### 3. Submit CI Jobs

```bash
# Submit all benchmarks to Redis/evaluator
crsbench benchmark ci all --all --distributed --redis-host localhost \
  --output-dir ci-results/

# Submit filtered subset
crsbench benchmark ci all --filter "afc-*" --distributed --redis-host redis.internal

# With coverage
crsbench benchmark ci all --all --distributed --redis-host localhost \
  --inc-coverage --output-dir ci-results/
```

The submitter will:
1. Run format validation locally (fast)
2. Build a single DAG
3. Enqueue all jobs once (build jobs route to build queue, verify/test jobs route to verify queue)
4. Rely on RQ dependencies for build-before-verify ordering
5. Poll until all jobs complete
6. Aggregate and print results

### Distributed Execution Flow

```
crsbench benchmark ci all --all --distributed
  |
  |-- Phase 1: Format validation (local, fast)
  |
  |-- Phase 2: Single DAG enqueue
  |     -> enqueue all jobs once
  |     -> build jobs route to build queue
  |     -> verify/test jobs route to verify queue
  |     -> RQ depends_on enforces ordering
  |     -> evaluator executes with configured parallelism
  |     -> poll until all complete
  |
  |-- Phase 3: Aggregate results + print table
```

### Environment Variables

| Variable | Description |
|----------|-------------|
| `CRSBENCH_REDIS_PASSWORD` | Redis password (if auth enabled; auto-loaded from `.env` when using valkey-helper) |

## DAG Job Types

The CI builds a flat DAG per benchmark. Each node is a job with explicit
dependencies.

```
Per benchmark:
  BuildSingleVariant(vulnerable, asan)  ─┐
  BuildSingleVariant(allpatched, asan)  ─┤
  BuildSingleVariant(cpv_0, asan)       ─┼── VerifyCpvPov(cpv_0, pov_0)
                                         ├── VerifyCpvVar(cpv_0, pov_1+)
                                         │
  BuildSingleVariant(vulnerable, asan)  ─┼── BuildPatchVariant(cpv_0, patch_0)
                                         │     ├── PatchPovTest(pov_0)
                                         │     ├── PatchVarTest(pov_1+)
                                         │     ├── PatchUnitTest(FULL)
  BuildSingleVariant(coverage)          ─┴── FlatCollectCoverage
```

- **Build jobs** have no dependencies (run first)
- **Verify/test jobs** depend on their build jobs
- All verify/test jobs for the same benchmark can run in parallel once builds
  complete
- Verify/test jobs consume prebuilt artifacts only. They do not trigger
  fallback builds on missing artifacts in distributed CI execution.
- Missing artifact/context errors fail only dependent jobs for that artifact;
  unrelated benchmark jobs continue.

## Output and Results

### Results Table

After completion, a summary table is printed:

```
Benchmark                       FMT  POV  PATCH  COV   Time
afc-curl-delta-01              PASS PASS  PASS  SKIP   45s
afc-curl-delta-02              PASS PASS  PASS  SKIP   38s
afc-zookeeper-delta-01         PASS PASS  PASS  SKIP   92s
```

### Output Directory Structure

When `--output-dir` is specified:

```
ci-results/
├── results.txt                # Human-readable report
├── summary.csv                # CSV summary
├── summary.json               # Full results JSON
└── {benchmark}/
    ├── summary.json           # Per-benchmark summary
    ├── errors.txt             # Failure details (when failed)
    ├── build/                 # Build job logs
    │   ├── *.log
    │   ├── *.stdout
    │   └── *.stderr
    └── verify/                # Verify/test job logs
        └── *.log
```

### Parsing Previous Results

```bash
crsbench benchmark ci parse --output-dir ci-results/
```

### Retrying Failed Benchmarks

```bash
crsbench benchmark ci retry --csv ci-results/summary.csv --output-dir ci-results-retry/
crsbench benchmark ci retry --csv ci-results/summary.csv --mode snapshot --output-dir ci-results-retry/
crsbench benchmark ci retry --csv ci-results/summary.csv --dry-run
```

## Source Mode

By default, CI uses `--source pkgs` which reads source tarballs from the
bundled `pkgs/` directory. This is the standard mode for reproducible builds.

To test with live git clones instead:

```bash
crsbench benchmark ci all --all --source main_repo
```

## Tips

- Start with `crsbench benchmark ci format --all` to catch structural issues fast
- Use `--filter` to test a subset before running all benchmarks
- For full parallel execution, use `--distributed` with Redis workers
- DAG local mode is sequential — for full parallelism, use `--distributed`
- Coverage is expensive and disabled by default; use `--inc-coverage` when needed
