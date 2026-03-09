# Distributed Robustness Validation (2026-03-07)

Purpose: prevent regressions in distributed CI/run workflows and separate infra bugs from benchmark ground-truth issues.

## Scope

- Queue lifecycle and polling robustness:
  - terminal status normalization (`str` vs enum-like status)
  - blocked deferred detection and recovery
  - stale started job handling
- Build-context correctness:
  - inc-build requested vs fallback-built variants
  - patch build context availability
- End-to-end command health:
  - `benchmark ci ... --distributed` + `evaluator --ci`
  - `run` + `worker` + `evaluator` for CRS workflows

## Infra Bug Scenarios

1. Terminal status type mismatch
- Risk: `finished` jobs not recognized, pending set never drains.
- Signal: submitter stalls while Redis jobs are already terminal.

2. Deferred jobs blocked by failed/missing dependencies
- Risk: deferred jobs remain indefinitely; DAG deadlocks.
- Signal: long waits with stable pending count + deferred backlog.

3. Stale started jobs (orphaned worker)
- Risk: jobs stay started forever without progress.
- Signal: old `started_at`, no completion, queue starvation.

4. Missing build context under inc-build fallback
- Risk: verify/patch jobs fail with `infra_missing_*_build_context`.
- Signal: build succeeded/fallback occurred but verify cannot load context.

5. Prepare-inc race / false-success propagation
- Risk: downstream jobs run with missing/invalid image state.
- Signal: immediate downstream missing image/context errors after prepare-inc.

## Unit Test Coverage Plan

## Added
- `tests/test_ci_jobs.py::test_poll_handles_enum_like_finished_status`
- `tests/test_ci_jobs.py::test_marks_deferred_job_immediately_when_dependency_failed_even_if_new`

## Existing tests to keep validating
- blocked deferred marking (`TestBlockedDeferredRecovery`)
- stale started handling (`TestEnqueueAndPollCiJobs` stale started tests)
- fallback build context loading (`test_fallback_load_accepts_non_inc_variants`)

## Planned additions
- end-to-end orphaned deferred recovery to finished result
- prepare-inc degraded/failure propagation semantics
- mixed-epoch build/verify stale-reuse consistency test

## Execution Matrix (Minimal, High-Signal)

Do NOT run all benchmarks during infra hardening.

### A) Distributed CI smoke (targeted benchmarks)

- Target set:
  - `atlanta-activemq-var-delta-01`
  - `atlanta-cron-utils-delta-01`
  - `atlanta-mosquitto-delta-01`

- Commands:
```bash
printf 'yes\n' | uv run python scripts/valkey-helper.py clean-all
uv run crsbench evaluator --ci --build-jobs 4 --build-cores-per-job 8 --verify-jobs 4 --verify-cores-per-job 8
uv run crsbench benchmark ci all \
  --benchmarks atlanta-activemq-var-delta-01 atlanta-cron-utils-delta-01 atlanta-mosquitto-delta-01 \
  --distributed --mode snapshot --force-rebuild --output-dir /tmp/ci-three-clean
```

- Success criteria:
  - `summary.csv` has `errors=0`.
  - Remaining failures (if any) are benchmark/GT-level only.

### B) Local CI smoke (no distributed queue path)

```bash
uv run crsbench benchmark ci all --benchmarks atlanta-cron-utils-delta-01 --mode full --output-dir /tmp/ci-local-cron
```

- Success criteria:
  - command exits cleanly; no infra exception.

### C) CRS run workflow smoke (non-all benchmarks)

Use one benchmark, one trial, address sanitizer, worker.jobs=1.

- Atlantis (`given_fuzzer`) baseline:
  - base config reference: `experiment-configs/experiment-config-sanity.yaml`
- Claude Code baseline:
  - base config reference: `experiment-configs/experiment-config-sanity-crs-claude-code-8c-2h.yaml`
- Codex baseline:
  - base config reference: `experiment-configs/experiment-config-afc-all-crs-codex-gpt-5-3-codex-delta.yaml`

Create constrained smoke variants from these references:
- `trials: 1`
- `benchmarks: [sanity-mock-c-delta-01]`
- `worker.jobs: 1`
- `sanitizers: [address]`
- short bounded timeouts

Run pattern:
```bash
uv run crsbench run --experiment-config <smoke-config.yaml>
uv run crsbench worker --experiment-config <smoke-config.yaml> --continuous
uv run crsbench evaluator --experiment-config <smoke-config.yaml>
```

### D) Distributed concurrency validation (run/worker/evaluator)

Goal: verify real parallel scheduling and CPU pinning in distributed mode.

Use a small config with multiple trials and worker/evaluator parallelism:
- `trials: 2`
- `worker.jobs: 2`, `resources.cores_per_trial: 2`
- evaluator: `--build-jobs 2 --build-cores-per-job 2 --verify-jobs 2 --verify-cores-per-job 2`

Commands:
```bash
printf 'yes\n' | uv run python scripts/valkey-helper.py clean-all
uv run crsbench evaluator --experiment-config /tmp/crsbench-sanity-concurrency-20260307.yaml \
  --build-jobs 2 --build-cores-per-job 2 --verify-jobs 2 --verify-cores-per-job 2 --idle-timeout 120
uv run crsbench worker --experiment-config /tmp/crsbench-sanity-concurrency-20260307.yaml \
  --continuous --jobs 2 --cores-per-job 2
uv run crsbench run --experiment-config /tmp/crsbench-sanity-concurrency-20260307.yaml \
  --distributed --queue-mode fresh
```

Evidence to require in logs:
- Evaluator side:
  - two build jobs start concurrently with different CPU sets (example: `0-1` and `2-3`)
  - lines like `Started build job ... with 2 CPUs: 0-1` and `... with 2 CPUs: 2-3`
- Worker side:
  - two trial jobs execute concurrently with different CPU sets
  - lines like `[Trial 1] ... Job assigned CPUs: 0-1` and `[Trial 2] ... Job assigned CPUs: 2-3`

Pass criteria:
- parallel jobs appear in both worker and evaluator logs with non-overlapping CPU sets.
- no infra queue deadlock signature during the run.

## Infra vs GT Classification Rules

- Infra error (must fix in CRSBench):
  - queue deadlock/stall, missing build context caused by orchestration mismatch,
    stale started/deferred lifecycle bugs, image/context mismatch from scheduler logic.
- GT quality issue (benchmark-side):
  - patch/POV semantics mismatch while infra paths are healthy (`errors=0`),
    duplicate/overlapping CPV patch design, benchmark-specific non-repro behavior.

## Mandatory Checks Before Commit

```bash
uv run ruff format crsbench/ tests/
scripts/ci-tests/run-local.sh checks
```
