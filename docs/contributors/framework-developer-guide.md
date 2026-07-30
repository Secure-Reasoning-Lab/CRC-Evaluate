# Framework Developer Guide

Use this guide when changing CRSBench framework/infrastructure code.

## Scope

Framework work includes changes under:

- `crsbench/`
- `scripts/`
- `services/`
- runtime orchestration and reporting paths

## Setup

```bash
git clone --recurse-submodules https://github.com/Secure-Reasoning-Lab/CRC-Evaluate.git && cd CRC-Evaluate
uv sync --extra dev
pre-commit install
```

## Quality Workflow

```bash
# tests
uv run pytest tests/ -v
uv run pytest tests/test_<module>.py -v

# checks
just check
just typecheck
just lint
just lint-fix
just format
```

Recommended order: tests -> typecheck -> lint/format.

## Development References

- Coding standards: [Coding Standards](./coding-standards.md)
- Distributed runtime: [Distributed Experiments](../deployment/distributed.md)
- CRS runtime interface: [OSS-CRS Interface](../reference/oss-crs-interface.md)
- Module docs: [Module Index](../modules/README.md)

## Pull Request Expectations

1. Keep changes scoped and reviewable
2. Update relevant docs in the same PR
3. Use clear commit messages (`fix:`, `feat:`, `docs:`, etc.)
