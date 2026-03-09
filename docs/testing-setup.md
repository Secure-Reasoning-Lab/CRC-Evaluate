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
scripts/setup-third-party.sh
```

## Fast Local Checks

```bash
# Typecheck + lint + format-check
scripts/ci-tests/run-local.sh checks

# Unit tests
uv run pytest tests/ -v
```

`run-local.sh checks` also enforces benchmark script path policy
(`SRC`/`OUT`/`WORK` contract, no `/built-src` or `/test-src`).

## Distributed/Runtime Checks

For Redis/Valkey setup and multi-process runs (`run`, `worker`, `evaluator`):

- [Experiment Workflow](./experiment-workflow.md)
- [Environment Setup](./environment-setup.md)
- [services/valkey/README.md](../services/valkey/README.md)

### Configless Evaluator Notes

- Running `crsbench evaluator` without `--experiment-config` discovers experiments from the Redis registry.
- It waits until at least one experiment is registered, then starts build/verify queue processing.
- In continuous mode, queue discovery is refreshed periodically, so newly
  registered experiments are adopted without restarting worker/evaluator.
- Startup does not enqueue pre-build jobs in configless mode; builds are consumed lazily from build queues.
- Multi-experiment configless mode requires shared `benchmarks_root` across discovered experiments.
- Evaluator resource sizing follows `CLI > experiment metadata (registry) > defaults`.
- Worker sizing follows the same precedence using `--jobs` / `--cores-per-job` and `worker.*` metadata.
- For numeric metadata conflicts without CLI overrides, runtime uses `max(...)`.
- CPU pinning is CLI-owned in distributed runtime (`--cpuset`, `--skip-cpuset`).
- Invalid registry numeric metadata is rejected at startup (`worker.* >= 1`, `resources.cores_per_trial >= 1`, `evaluator.build/verify_* >= 1`, `evaluator.idle_timeout >= 0`).
- `resources.memory_per_trial` default is `null` (unlimited).

## Integration Test Scripts

- Smoke/local CI runner: `scripts/ci-tests/run-local.sh`
