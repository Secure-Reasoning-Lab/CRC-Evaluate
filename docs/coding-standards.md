# Coding Standards

Use these standards for CRSBench framework code.

## Scope

- Applies to `crsbench/`, `scripts/`, and related framework code paths.
- Benchmark-specific policy and lifecycle guidance is in `docs/benchmark-developer-guide.md`.

## Python Style

- Use absolute imports.
- Do not use `print()` for framework/runtime output; use `crsbench.utils.logger.get_logger`.
- Keep functions focused and small; avoid deep nesting by using early returns.
- Prefer clear names over abbreviations.
- Avoid bare `except`; catch specific exceptions.
- Prefer `pathlib` over `os.path`.

## Signatures and Types

- Keep parameter lists short; remove unused arguments.
- Pass boolean arguments as keywords.
- Use concrete type annotations and avoid `Any` unless unavoidable.
- Use Pydantic v2 validators (`@field_validator`, `@model_validator`) in new code.

## Tests and Quality Gates

Run checks in this order:

1. `uv run pytest tests/test_<module>.py -v`
2. `just typecheck`
3. `just lint` (or `just lint-fix`)
4. `just format`

For full validation, run `just check` after tests.

## Code Organization

- Keep `crsbench/run_experiment.py` as the CLI entrypoint at package root.
- Place new functionality in focused submodules (for example, `crsbench/validation/`).
- Keep docs updated in the same PR when behavior changes.
