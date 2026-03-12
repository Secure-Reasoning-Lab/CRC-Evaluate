# Experiment Config Reference

The grouped experiment YAML contract is documented in:

- [Distributed experiment config example](../../experiment-config-distributed-example.yaml)

Use that file as the configuration source of truth for field layout, comments,
and examples.

## Core Contract

- `experiment`: task, mode, suite/benchmarks, sanitizers
- `runtime`: trials, timeouts, Redis host, LiteLLM settings, inputs
- `storage`: experiment/report/result storage paths
- `crs_compose`: CRS services and per-CRS runtime resources
- `worker` and `evaluator`: machine-local execution defaults
- `resources`: fallback per-trial resource defaults

## Input Contract

`runtime.inputs` is presence-based:

- `pov`
- `sarif`
- `seed`
- `diff`

If a key is absent, it is disabled. If present, it is enabled and validated
according to its fields.

## Incremental Image Settings

Incremental benchmark image settings are top-level experiment-config fields:

- `inc_image_policy`
- `inc_image_registry`
- `inc_image_max_pull_bytes`
- `inc_image_pull_timeout_sec`
- `project_image_prefix`

These settings control evaluator/build-side snapshot image preparation.

Prepare order:

- use the local inc-build image if already present
- otherwise, if policy allows pull, try `docker pull`
- otherwise, or if pull fails and policy allows local build, build locally
- if incremental image preparation still fails, callers fall back to standard
  non-inc builds

Policy meanings:

- `auto`: local -> pull -> local build fallback
- `pull_only`: local -> pull only
- `build_only`: local -> local build only

Size gate:

- `inc_image_max_pull_bytes` is a remote manifest size cap
- if the remote size is known and exceeds the cap, CRSBench skips the pull
- if the remote size cannot be determined, current behavior is fail-open and a
  pull may still be attempted

Pull timeout:

- `inc_image_pull_timeout_sec` is propagated to the `docker pull` timeout

Evaluator mode note:

- config-pinned evaluator CLI mode normally performs a startup pre-build
  enqueue phase
- configless evaluator mode does not enqueue startup pre-builds; it consumes
  build jobs lazily and verify work may trigger on-demand builds on cache miss

## Legacy-to-Grouped Field Mapping

Current experiment configs should use the grouped contract. Older flat keys map
to the grouped layout as follows:

| Old shape | Current shape |
|---|---|
| `trials` | `runtime.trials` |
| `max_total_time` | `runtime.max_total_time` |
| `build_timeout` | `runtime.build_timeout` |
| `run_timeout` | `runtime.run_timeout` |
| `verify_timeout` | `runtime.verify_timeout` |
| `per_pov_verify_timeout` | `runtime.per_pov_verify_timeout` |
| `experiment_filestore` | `storage.experiment_filestore` |
| `report_filestore` | `storage.report_filestore` |
| `keep_only_results` | `storage.keep_only_results` |
| `cleanup_after_trial` | `storage.cleanup_after_trial` |
| `copy_results_after_trial` | `storage.copy_results_after_trial` |
| `results_filestore` | `storage.results_filestore` |
| `crses` / `oss_crs_registry` | `crs_compose` service keys |
| `litellm_mode` | `runtime.litellm.mode` |
| `llm_tracking_enabled` | `runtime.litellm.tracking_enabled` |
| `skip_litellm` | `runtime.litellm.skip` |
| `seed_corpus_enabled` / `seed_corpus_max_time` | `runtime.inputs.seed` / `runtime.inputs.seed.max_time` |
| `hints_enabled` / `hint_sarif_level` | `runtime.inputs.sarif.level` |

Notes:
- `adapter`, `difficulty_level`, and top-level `crses` are not part of the
  current grouped contract for new configs.
- `difficulty_level` remains benchmark metadata in the RFC, not an experiment
  config field.
- Some legacy top-level keys are still normalized for compatibility, but new
  configs should not introduce them.

## Compatibility Notes

- `runtime.inputs.seed` replaces the older seed-corpus knobs documented in
  [Seed Corpus Reference](../../reference/seed-corpus.md).
- `runtime.inputs.sarif` replaces the older hint-level knobs. Generated SARIF
  artifacts still come from benchmark-owned `level_N.sarif` files.
- `runtime.litellm.*` replaces the old flat LiteLLM flags.
- `pov_dedup_strategy` remains a top-level experiment-config field when
  applicable; it is not currently grouped under `runtime`.

## Worker Filesystem Overrides

Use worker-local overrides only when remote workers mount paths differently from
the orchestrator:

- `worker.benchmarks_root`
- `worker.storage.experiment_filestore`
- `worker.storage.report_filestore`
- `worker.storage.results_filestore`

## Related

- Distributed workflow: [distributed.md](./distributed.md)
- First experiment: [../../getting-started/first-experiment.md](../../getting-started/first-experiment.md)
