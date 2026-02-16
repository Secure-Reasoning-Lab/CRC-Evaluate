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

```bash
crsbench ci format --all          # Validate format (no Docker)
crsbench ci build  --all          # Build variant images
crsbench ci pov    --all          # Verify ground-truth POVs
crsbench ci patch  --all          # Verify ground-truth patches
crsbench ci all    --all          # Run all checks
crsbench ci build  --all --distributed  # Distributed builds
```
