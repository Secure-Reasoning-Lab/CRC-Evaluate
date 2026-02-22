# Codex Repository Instructions

## Pre-Commit Quality Gate

Before creating any commit, run:

```bash
ci-tests/run-local.sh checks
```

If this command fails, do not commit until failures are fixed.

## Formatting Requirement

When touching Python files under `crsbench/` or `tests/`, ensure formatting is clean:

```bash
uv run ruff format crsbench/ tests/
```

Then re-run the quality gate command above.
