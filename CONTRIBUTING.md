# Contributing to CRSBench

## Setup

```bash
git clone https://github.com/sslab-gatech/CRSBench.git && cd CRSBench
uv sync --extra dev
pre-commit install
```

## Code Quality

```bash
# Run tests first
uv run pytest tests/ -v
uv run pytest tests/test_<module>.py -v   # single module

# Then checks
just check       # typecheck + lint + format (all at once)
just typecheck   # type checking only
just lint        # linting only
just lint-fix    # auto-fix lint issues
just format      # auto-format code
```

**Workflow**: tests → typecheck → lint/format.

## Coding Standards

See [CLAUDE.md](CLAUDE.md) for the full coding standards, including:

- Import style (absolute imports only)
- Logging (`from crsbench.utils.logger import get_logger`, never `import logging`)
- Naming conventions, function design, nesting limits
- Type annotations (avoid `Any`, use type aliases)
- Testing conventions (`uv run pytest`, tests in `tests/`)

## Documentation

- Create a design doc in `design-docs/<module>/` before implementing new features
- Update module README at `crsbench/<module>/README.md`
- User-facing docs go in `docs/`

## Project Layout

Each major feature lives in its own module under `crsbench/`:

| Module | Purpose |
|--------|---------|
| `builder/` | OSS-Fuzz variant building |
| `evaluation/` | CRS execution & verification |
| `benchmark_ci/` | Benchmark CI pipeline |
| `distributed/` | Multi-machine execution (Redis/RQ) |
| `benchmark/` | Packaging, canary, seed tools |
| `dataset/` | HuggingFace upload/download |
| `validation/` | Format validation & schemas |
| `migration/` | Format migration tools |
| `hint_generation/` | Progressive hint generation |
| `reporting/` | Reports & dashboard |
| `statistics/` | Benchmark statistics |
| `utils/` | Shared utilities (logger, YAML, etc.) |

Only `run_experiment.py` (CLI entry point) lives at the `crsbench/` root.

## Benchmark Management

```bash
crsbench benchmark validate      benchmarks/project
crsbench benchmark bundle        benchmarks/project
crsbench benchmark bundle-all    benchmarks/ --workers 8
crsbench benchmark prepare-delta benchmarks/project
crsbench benchmark inject-canary benchmarks/ --filter "atlanta-*"
crsbench benchmark upload        --dataset crsbench       # HuggingFace upload
crsbench benchmark upload        --dataset crsbench --dry-run
```

## Benchmark CI

### Local (single machine)

```bash
crsbench ci format --all          # Validate format (no Docker)
crsbench ci build  --all          # Build variant images
crsbench ci pov    --all          # Verify ground-truth POVs
crsbench ci patch  --all          # Verify ground-truth patches
crsbench ci all    --all          # Run all checks
```

### Distributed (with Redis/Valkey)

Distributed CI splits work into build and verify jobs processed by evaluator
workers. This lets you parallelize across many cores or multiple machines.

**Start Valkey/Redis first:**

```bash
python scripts/valkey-helper.py --password start
```

This starts a Valkey instance and saves the connection password to `.env`.

**Terminal 1 — Submit jobs:**

```bash
# Submit all benchmarks (enqueues build + verify jobs to Redis)
crsbench ci all --all --distributed --output-dir ci-results

# Skip incremental build, always do full builds
crsbench ci all --all --distributed --output-dir ci-results --no-inc-build

# Also skip force-rebuild (reuse existing Docker images)
crsbench ci all --all --distributed --output-dir ci-results \
  --no-inc-build --no-force-rebuild
```

**Terminal 2 — Start evaluator to process jobs:**

```bash
crsbench evaluator --ci \
  --build-jobs 16 --build-cores-per-job 8 \
  --verify-jobs 16 --verify-cores-per-job 8
```

The submitter enqueues jobs to Redis queues. The evaluator dequeues and
executes them in parallel. Build jobs run first, then verify/patch/test jobs.

**Core allocation:** The evaluator distributes available CPU cores across
build and verify jobs. For example, on a 128-core machine:

```bash
# 16 build jobs × 8 cores = 128 cores for builds
# After builds finish, 16 verify jobs × 8 cores = 128 cores for verification
crsbench evaluator --ci \
  --build-jobs 16 --build-cores-per-job 8 \
  --verify-jobs 16 --verify-cores-per-job 8

# Pin to specific cores (e.g., cores 0-63 only)
crsbench evaluator --ci \
  --build-jobs 8 --build-cores-per-job 8 \
  --verify-jobs 16 --verify-cores-per-job 4 \
  --cores 0-63
```

See `crsbench/benchmark_ci/README.md` for the full option reference and
execution flow.
