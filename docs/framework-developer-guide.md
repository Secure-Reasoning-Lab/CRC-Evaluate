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
git clone https://github.com/sslab-gatech/CRSBench.git && cd CRSBench
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

- Coding standards: `docs/coding-standards.md`
- Distributed runtime: `docs/experiment-workflow.md`
- CRS runtime interface: `docs/ossfuzz-crs-interface.md`
- Design docs: `docs/design/README.md`
- Module docs: `docs/modules/README.md`

## Pull Request Expectations

1. Keep changes scoped and reviewable
2. Update relevant docs in the same PR
3. Use clear commit messages (`fix:`, `feat:`, `docs:`, etc.)
