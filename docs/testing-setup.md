# Testing Setup

Use this guide for day-to-day CRSBench testing. It is intentionally short and
points to canonical docs to avoid drift.

## Prerequisites

- Python 3.11+
- `uv`
- Docker

## Install

```bash
git clone https://github.com/sslab-gatech/CRSBench.git
cd CRSBench
uv sync --extra dev
```

## Fast Local Checks

```bash
# Typecheck + lint + format-check
ci-tests/run-local.sh checks

# Unit tests
uv run pytest tests/ -v
```

## Distributed/Runtime Checks

For Redis/Valkey setup and multi-process runs (`run`, `worker`, `evaluator`):

- [Experiment Workflow](./experiment-workflow.md)
- [Environment Setup](./environment-setup.md)
- [services/valkey/README.md](../services/valkey/README.md)

## Integration Test Scripts

- [integration_tests/README.md](../integration_tests/README.md)
- [integration_tests_distributed/README.md](../integration_tests_distributed/README.md)
