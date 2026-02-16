# Benchmark CI

End-to-end testing for CRSBench benchmarks: format validation, build, POV
verification, patch verification, unit tests (full + RTS), and coverage.

## What Gets Tested

| Stage | What | Docker? |
|-------|------|---------|
| **Format** | meta.yaml, project.yaml, Dockerfile, POV files, patches | No |
| **Build** | Build vulnerable, allpatched, and per-CPV variants | Yes |
| **POV** | POVs trigger correct CPVs on vulnerable variant | Yes |
| **Patch** | Ground-truth patches fix vulnerabilities | Yes |
| **Unit Test (FULL)** | test.sh passes on patched variant | Yes |
| **Unit Test (RTS)** | RTS-selected tests pass (if project supports RTS) | Yes |
| **Coverage** | Coverage collection on vulnerable variant (optional) | Yes |

## Quick Start

### Prerequisites

```bash
# Install crsbench in editable mode
uv pip install -e .

# Docker must be running (for build/verify stages)
docker info
```

### Run All Checks on a Single Benchmark

```bash
crsbench ci all benchmarks/afc-curl-delta-01
```

### Run All Checks on All Benchmarks

```bash
crsbench ci all --all
```

## CLI Reference

### Subcommands

```bash
crsbench ci format    # Format validation only (no Docker)
crsbench ci build     # Build variants only (no verification)
crsbench ci pov       # Build + POV verification
crsbench ci patch     # Build + patch verification + unit tests
crsbench ci coverage  # Build + coverage collection
crsbench ci rts       # Build + RTS unit test checks
crsbench ci all       # All of the above (coverage with --inc-coverage)
crsbench ci parse     # Parse results from a previous run
crsbench ci retry     # Retry failed benchmarks from a previous run
crsbench ci storage   # Show storage usage per benchmark (no Docker)
```

### Benchmark Selection

All subcommands (except `parse`) accept:

```bash
# Single benchmark (positional)
crsbench ci all benchmarks/afc-curl-delta-01

# Multiple benchmarks (comma-separated)
crsbench ci all -b afc-curl-delta-01,afc-curl-delta-02,afc-tika-delta-01

# Filter by glob pattern
crsbench ci all --filter "afc-curl-*"
crsbench ci all --filter "atlanta-*"

# All benchmarks
crsbench ci all --all

# From a suite file
crsbench ci all -s smoke-test-bug-finding
```

### Common Options

```bash
--source {pkgs,main_repo}   # Source mode (default: pkgs = bundled tarballs)
--no-inc-build               # Force full build (default uses inc-build)
--force-rebuild              # Rebuild even if cached (default ON for CI)
--exit-on-error              # Stop on first failure
--output-dir DIR             # Save per-job logs and artifacts
--output FILE                # Save summary JSON
--no-color                   # Disable colored output
--max-povs-per-cpv N         # Limit POVs verified per CPV
--controller-cores N         # CPU cores reserved for controller (default: 2)
```

## Local Execution

By default, `crsbench ci` runs locally in a single process. Jobs are
topologically sorted by dependencies and executed **sequentially**.

```bash
# Run all checks on all benchmarks (sequential)
crsbench ci all --all --output-dir ci-results/

# Format check only (fast, no Docker)
crsbench ci format --all

# Build only (no verification)
crsbench ci build --all

# POV verification only
crsbench ci pov --filter "afc-curl-*"

# Patch + unit tests
crsbench ci patch --filter "afc-zookeeper-*"

# Include coverage (expensive)
crsbench ci all --all --inc-coverage --output-dir ci-results/
```

**Note**: In local mode, `--build-workers` and `--verify-workers` are accepted
but **not used** — execution is always sequential. Use distributed mode for
parallel execution.

### Execution Flow (Local)

```
crsbench ci all --all
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

### Architecture

```
                      +-----------------+
                      |  crsbench ci    |
                      |  --distributed  |
                      |  (submitter)    |
                      +--------+--------+
                               |
                    enqueue jobs to Redis
                               |
                      +--------v--------+
                      |     Redis       |
                      |  build queue    |
                      |  verify queue   |
                      +--------+--------+
                               |
              +----------------+----------------+
              |                |                |
     +--------v------+ +------v--------+ +-----v---------+
     | crsbench      | | crsbench      | | crsbench      |
     | evaluator     | | evaluator     | | evaluator     |
     | --ci          | | --ci          | | --ci          |
     | (machine 1)   | | (machine 2)   | | (machine 3)   |
     +---------------+ +---------------+ +---------------+
```

The submitter builds the DAG, serializes jobs, enqueues them to Redis, then
polls until all complete. Workers dequeue and execute jobs independently.

### Step-by-Step Setup

#### 1. Start Redis

```bash
# Using Docker
docker run -d --name redis -p 6379:6379 redis:7

# Or system Redis
redis-server
```

#### 2. Start Worker(s)

Each worker runs a dual-queue supervisor that processes build jobs (priority)
and verify/test jobs concurrently.

```bash
# Single machine, 10 build slots + 20 verify slots
crsbench evaluator --ci --redis-host localhost \
  --build-jobs 10 --build-cores-per-job 2 --verify-jobs 20 \
  --continuous

# With CPU affinity (pin to specific cores)
crsbench evaluator --ci --redis-host localhost \
  --build-jobs 8 --build-cores-per-job 4 --verify-jobs 16 \
  --cores 0-63 --skip-cpus 0-3 --continuous

# Multiple machines (each connects to same Redis)
# Machine A (64 cores):
crsbench evaluator --ci --redis-host redis.internal \
  --build-jobs 8 --build-cores-per-job 4 --verify-jobs 32 \
  --cores 64 --continuous

# Machine B (32 cores):
crsbench evaluator --ci --redis-host redis.internal \
  --build-jobs 4 --build-cores-per-job 4 --verify-jobs 16 \
  --cores 32 --continuous
```

**Worker options:**

| Flag | Description | Default |
|------|-------------|---------|
| `--ci` | Use CI queue mode (build + verify) | Required |
| `--redis-host HOST` | Redis server hostname | localhost |
| `--build-jobs N` | Max concurrent build jobs | value of -j |
| `--build-cores-per-job M` | CPUs per build job | 1 |
| `--verify-jobs K` | Max concurrent verify/test jobs | build-jobs * cores |
| `--cores CORES` | CPU count or cpuset range (e.g., `0-63`) | all |
| `--skip-cpus CPUSET` | CPUs to exclude (e.g., `0-3`) | none |
| `--continuous` | Keep running after queue drains | off |
| `--worker-name NAME` | Identifier for this worker | hostname |

#### 3. Submit CI Jobs

```bash
# Submit all benchmarks to Redis workers
crsbench ci all --all --distributed --redis-host localhost \
  --output-dir ci-results/

# Submit filtered subset
crsbench ci all --filter "afc-*" --distributed --redis-host redis.internal

# With coverage
crsbench ci all --all --distributed --redis-host localhost \
  --inc-coverage --output-dir ci-results/
```

The submitter will:
1. Run format validation locally (fast)
2. Build the DAG
3. Enqueue build jobs to Redis build queue via `VariantPlanner`
4. Poll until all builds complete
5. Enqueue verify/patch/unittest/RTS jobs to Redis verify queue
6. Poll until all verify jobs complete
7. Aggregate and print results

### Distributed Execution Flow

```
crsbench ci all --all --distributed
  |
  |-- Phase 1: Format validation (local, fast)
  |
  |-- Phase 2a: Build phase
  |     VariantPlanner creates build jobs
  |     -> enqueue to Redis build queue
  |     -> workers build Docker images in parallel
  |     -> poll until all complete
  |
  |-- Phase 2b: Verify phase
  |     Remaining jobs (POV, patch, unittest, RTS, coverage)
  |     -> enqueue to Redis verify queue
  |     -> workers execute in parallel
  |     -> poll until all complete
  |
  |-- Phase 3: Aggregate results + print table
```

### Environment Variables

| Variable | Description |
|----------|-------------|
| `REDIS_PASSWORD` | Redis password (if auth enabled) |
| `CRSBENCH_BUILD_WORKERS` | Default build workers (overridden by CLI) |
| `CRSBENCH_VERIFY_WORKERS` | Default verify workers (overridden by CLI) |
| `CRSBENCH_CONTROLLER_CORES` | CPU cores for controller monitoring |

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
                                         │     └── PatchUnitTest(RTS)
                                         │
  BuildSingleVariant(coverage)          ─┴── FlatCollectCoverage
```

- **Build jobs** have no dependencies (run first)
- **Verify/test jobs** depend on their build jobs
- All verify/test jobs for the same benchmark can run in parallel once builds
  complete

## Output and Results

### Results Table

After completion, a summary table is printed:

```
Benchmark                       FMT  POV  PATCH  RTS  COV   Time
afc-curl-delta-01              PASS PASS  PASS  PASS SKIP   45s
afc-curl-delta-02              PASS PASS  PASS  PASS SKIP   38s
afc-zookeeper-delta-01         PASS PASS  PASS  PASS SKIP   92s
```

### Output Directory Structure

When `--output-dir` is specified:

```
ci-results/
├── summary.json               # Full results JSON
├── results.csv                # CSV summary
└── {benchmark}/
    └── {job-id}/
        ├── stdout.log
        └── stderr.log
```

### Parsing Previous Results

```bash
crsbench ci parse ci-results/
```

### Retrying Failed Benchmarks

```bash
crsbench ci retry ci-results/ --output-dir ci-results-retry/
```

## Source Mode

By default, CI uses `--source pkgs` which reads source tarballs from the
bundled `pkgs/` directory. This is the standard mode for reproducible builds.

To test with live git clones instead:

```bash
crsbench ci all --all --source main_repo
```

## Tips

- Start with `crsbench ci format --all` to catch structural issues fast
- Use `--filter` to test a subset before running all benchmarks
- Use `--exit-on-error` for fast feedback during development
- For full parallel execution, use `--distributed` with Redis workers
- Local mode is sequential — for parallelism, distributed mode is required
- Coverage is expensive and disabled by default; use `--inc-coverage` when needed
