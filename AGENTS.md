# Codex Repository Instructions

## Engineering Approach

- Do not implement temporary workarounds when fixing issues.
- Address the root cause systematically with durable, maintainable changes.

## Rule Priority

When instructions conflict, prioritize in this order:

1. Correctness
2. Reliability
3. Performance
4. Developer convenience

## Scalability and Environment Scope

- Design and validate changes for scalability beyond local execution.
- Consider behavior on remote workers, cloud systems, and distributed environments.

## Testing Strategy

- Prioritize functional happy-path test cases first.
- Add edge-case tests after happy paths are covered.
- Add failure-mode and regression tests for the root cause after happy paths and edge cases.
- If a change affects queue/worker/distributed execution, validate at least one non-local path (remote worker, cloud environment, or distributed setup).

## Code Quality and Readability

- Keep code structured, consistent, and easily readable for humans.

## Documentation Maintenance

- When behavior, interfaces, or workflows change, update the nearest relevant docs under `docs/`.
- If doc entry points change, update the `Docs Index (Agent Jump List)` in this file.

## Definition of Done

Before considering work complete:

- Implementation addresses root cause (not a temporary workaround).
- Relevant tests are added/updated and pass (happy path first, then edge/failure/regression as needed).
- Distributed/non-local validation is completed when applicable.
- Relevant docs are updated when behavior or workflows changed.
- Pre-commit quality gate passes: `scripts/ci-tests/run-local.sh checks`.

## Pre-Commit Quality Gate

Before creating any commit, run:

```bash
scripts/ci-tests/run-local.sh checks
```

If this command fails, do not commit until failures are fixed.

## Formatting Requirement

When touching Python files under `crsbench/` or `tests/`, ensure formatting is clean:

```bash
uv run ruff format crsbench/ tests/
```

Then re-run the quality gate command above.

## Docs Index (Agent Jump List)

- Start here: `docs/README.md`
- Architecture: `docs/design/architecture.md`
- Distributed systems: `docs/design/distributed/distributed-evaluation.md`
- Job queue/workers: `docs/design/distributed/distributed-job-queue.md`
- Deployment/cloud: `docs/design/distributed/deployment-guide.md`
- Evaluation flow: `docs/design/evaluation/evaluation.md`
- Testing setup: `docs/testing-setup.md`
- Coding standards: `docs/coding-standards.md`
