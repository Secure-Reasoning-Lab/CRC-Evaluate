# Next Session Handoff (2026-03-07)

Purpose: reduce context loss and make continuation deterministic.

## Current state

- Distributed CI infra fixes landed for:
  - terminal status normalization
  - blocked deferred recovery logic
  - stale/deferred queue handling hardening
- Targeted runs reached infra-stable state (`errors=0`) for focused sets.
- Remaining failures are benchmark/GT side unless new infra signatures appear.

## Do first

1. Clean queue state:
```bash
printf 'yes\n' | uv run python scripts/valkey-helper.py clean-all
```
2. Start evaluator:
```bash
uv run crsbench evaluator --ci --build-jobs 4 --build-cores-per-job 8 --verify-jobs 4 --verify-cores-per-job 8
```
3. Run focused CI:
```bash
uv run crsbench benchmark ci all \
  --benchmarks atlanta-activemq-var-delta-01 atlanta-cron-utils-delta-01 atlanta-mosquitto-delta-01 \
  --distributed --mode snapshot --force-rebuild --output-dir /tmp/ci-three-clean
```

## Expected signals

- Healthy infra:
  - no `infra_missing_*_build_context`
  - no queue deadlock with static pending and active workers absent
  - summary: `errors=0`
- Likely GT-side:
  - patch/POV semantic mismatch with healthy infra execution
  - project-specific CPV overlap/design issues

## Mandatory distributed concurrency check (run/worker/evaluator)

Before broad runs, validate concurrency with a small experiment:

```bash
printf 'yes\n' | uv run python scripts/valkey-helper.py clean-all
uv run crsbench evaluator --experiment-config /tmp/crsbench-sanity-concurrency-20260307.yaml \
  --build-jobs 2 --build-cores-per-job 2 --verify-jobs 2 --verify-cores-per-job 2 --idle-timeout 120
uv run crsbench worker --experiment-config /tmp/crsbench-sanity-concurrency-20260307.yaml \
  --continuous --jobs 2 --cores-per-job 2
uv run crsbench run --experiment-config /tmp/crsbench-sanity-concurrency-20260307.yaml \
  --distributed --queue-mode fresh
```

Required evidence:
- evaluator logs show simultaneous starts on distinct cpusets (`0-1`, `2-3`)
- worker logs show concurrent trial execution with distinct cpusets

## Known benchmark caveats

- `atlanta-activemq-var-delta-01`:
  - ensure CPV/POV layout consistency for `ActivemqVariantOne` and `ActivemqVariantOneFDP`.
- `atlanta-cron-utils-delta-01`:
  - prior false-negative behavior can be GT/patch-design dependent.
- `atlanta-mosquitto-delta-01`:
  - patch applicability/semantic mismatch can remain even when infra is healthy.

## If CI stalls

1. Check active RQ workers and queue depth.
2. Check for stale `STARTED/DEFERRED` jobs.
3. Re-run queue clean + evaluator + focused CI.
4. Treat as infra bug only if reproducible with clean queue and fresh evaluator.

## Command set for CRS workflow smoke (non-CI)

- Use a constrained experiment config (`trials: 1`, one benchmark, one sanitizer).
- Run:
```bash
uv run crsbench evaluator --experiment-config <cfg.yaml> --build-jobs 1 --build-cores-per-job 2 --verify-jobs 1 --verify-cores-per-job 2
uv run crsbench worker --experiment-config <cfg.yaml> --continuous --jobs 1 --cores-per-job 2
uv run crsbench run --experiment-config <cfg.yaml> --distributed --queue-mode fresh
```

## Pre-commit gate

```bash
uv run ruff format crsbench/ tests/
scripts/ci-tests/run-local.sh checks
```
