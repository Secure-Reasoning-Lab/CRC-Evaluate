# Experiment Config Reference

The grouped experiment YAML contract is documented in:

- [Distributed experiment config example](../../experiment-config-distributed-example.yaml)

Use that file as the configuration source of truth for field layout, comments,
and examples.

## Core Contract

- `experiment`: task, mode, suite/benchmarks, sanitizers
- `runtime`: trials, timeouts, Redis host, LiteLLM settings, inputs
- `storage`: experiment/report/result storage paths
- `crs_compose`: CRS services and per-CRS runtime resources, including
  per-CRS `budget_policy` (`continue` | `terminate`) governing behavior when
  the trial LLM budget is exceeded — see
  [Evaluation design: Trial Budget Policy](../../design/evaluation/evaluation.md#trial-budget-policy)
- `worker` and `evaluator`: machine-local execution defaults
- `resources`: fallback per-trial resource defaults
- `cloud`: optional provider-neutral cloud placement contract

The `cloud` section is intentionally provider-neutral so future managed backends
can share one top-level shape. Today the only implemented managed backend is
GCE, so current launchable configs use `cloud.providers.gce`.

## Cloud Contract

Managed cloud execution uses a provider-neutral top-level shape:

- `cloud.providers.<provider>`: provider-native backing details such as GCE
  project, reusable instance profiles, optional provider `defaults`, optional
  `profile_defaults`, and optional default `region` / ordered `regions` plus
  ordered `zones` / `fallback` placement policy
- `cloud.defaults`: provider-agnostic launch/bootstrap defaults such as
  `readiness_timeout_sec`, `crsbench_install_spec`, `crsbench_git_ref`, and
  `github_deploy_key_path`
- `cloud.remote.experiment_root`: remote-VM experiment root used by
  `cloud collect` / `cloud teardown`; defaults to
  `storage.experiment_filestore` when unset for backward compatibility
- `cloud.env`: global environment variables merged into all launched cloud roles;
  this is also the top-level place to set startup-script overrides such as
  `CRSBENCH_TIMEZONE`
- `cloud.orchestrator`: instance-profile reference for the remote orchestrator
  VM, plus optional orchestrator-only `region`, `regions`, `zones`,
  `fallback`, and `env`
- `cloud.workers.defaults`: optional role-level worker placement defaults such
  as `count`, `instance_profile`, `region`, `regions`, `zones`, `fallback`,
  and `env`
- `cloud.workers.placements[]`: explicit worker placements with optional
  `region` / `regions` / `zones` / `fallback` overrides plus any inherited
  defaults
- `cloud.evaluators.defaults`: optional role-level evaluator placement defaults
  such as `count`, `instance_profile`, `region`, `regions`, `zones`,
  `fallback`, and `env`
- `cloud.evaluators.placements[]`: optional evaluator placements with optional
  `region` / `regions` / `zones` / `fallback` overrides plus any inherited
  defaults
For GCE in v1:

- use `cloud.providers.gce`
- launch/bootstrap defaults merge as
  `cloud.defaults -> cloud.providers.gce.defaults`
- provider-neutral configs do not repeat `provider` on orchestrator or
  placements; CRSBench resolves the provider from the owning
  `cloud.providers.<provider>.instance_profiles` catalog
- instance-profile keys must be globally unique across provider catalogs
- one launch cannot mix providers across orchestrator, workers, and evaluators
- effective zone order resolves as placement/orchestrator `zones` override,
  then role defaults, then `cloud.providers.gce.zones`
- effective region order resolves as placement/orchestrator `regions`
  override, then singular `region`, then role defaults, then
  `cloud.providers.gce.regions`, then singular `cloud.providers.gce.region`
- effective fallback resolves as placement/orchestrator `fallback` override,
  then role defaults, then `cloud.providers.gce.fallback`, then `true`
- when an effective region order is present, CRSBench uses GCE regional bulk
  insert with `ANY_SINGLE_ZONE`; optional `zones` are treated as an allowlist
  across those regions rather than an ordered retry list
- config validation rejects any `zones` entry whose region does not match one
  of the effective `regions`
- runtime fallback uses declared `regions` order for recognized regional
  capacity failures, or declared `zones` order for recognized zonal capacity
  failures; `fallback: false` hard-fails that logical placement and rolls back
  the launch
- live quota validation is mandatory before launch
- worker and evaluator placements can use different instance profiles
- `ssh_via_iap` controls operator SSH transport; `assign_external_ip` controls
  whether GCE attaches ephemeral external NAT for outbound internet access
- cloud env merge order is:
  `cloud.env -> profile_defaults.env -> instance_profile.env -> role/default placement env`
- runtime-managed env such as Redis connection material is applied after those
  user-configured layers and wins last
- generated GCE instance names are deterministic and zone-independent:
  `crsbench-<experiment>-orch`, `-work-001`, `-eval-001`
- the checked-in examples are
  `experiment-configs/cloud-testing/gce-sanity-1orch-2worker-1eval.yaml`
  and
  `experiment-configs/cloud-testing/gce-sanity-1orch-2worker-1eval-multilang-given-fuzzer.yaml`
  and
  `experiment-configs/cloud-testing/gce-usenix-r1-1orch-2worker-1eval-multilang-given-fuzzer.yaml`
  and
  `experiment-configs/cloud-testing/gce-hf-download-1orch-2worker-1eval.yaml`
  and
  `experiment-configs/cloud-testing/gce-sanity-zone-fallback-1orch-1worker-1eval.yaml`
  and
  `experiment-configs/cloud-testing/gce-sanity-zone-1orch-2worker-1eval.yaml`

## Discovery-Only Mode

Run against OSS-Fuzz-format projects without ground truth CPVs.

### Quick Start

Point `benchmarks` at any OSS-Fuzz project directory (one with
`project.yaml`, `Dockerfile`, `build.sh`). CRSBench auto-generates
`.aixcc/meta.yaml` on first run by building the project and discovering
fuzz targets:

```yaml
experiment:
  name: my-discovery-run
  mode: full
  benchmarks:
    - libyang     # raw OSS-Fuzz project — meta.yaml auto-generated
  only_cpv_harnesses: false
runtime:
  trials: 1
  max_total_time: 3600
  skip_verification: true
  skip_litellm: true
```

Auto-generation uses `oss_fuzz_path` (default: `third_party/oss-fuzz`)
to build the project via `helper.py build_fuzzers`. The generated
`meta.yaml` is persisted so subsequent runs skip the build.

### Config Fields

- `only_cpv_harnesses: false` — include harnesses regardless of CPV
  availability. Applies to both bug-finding and bug-fixing CRS types.
- `skip_verification: true` — skip POV/patch verification (no ground
  truth to verify against).

CRS runs proceed normally, POVs and patches are collected as artifacts,
and reports show raw discovery counts without CPV-based scoring.

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
  build jobs lazily and async POV verification enqueues benchmark-local build
  jobs on first POV discovery, with verify jobs waiting on those dependencies
- `runtime.verify_timeout` is the overall async verification drain budget after
  a trial ends; it covers both queued build prerequisites and queued POV
  verification jobs

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

Avoid using `/tmp` for persisted storage roots such as
`storage.experiment_filestore`, `storage.report_filestore`,
`storage.results_filestore`, `worker.storage.*`, or
`cloud.remote.experiment_root`. On Linux these paths are often backed by
`tmpfs`, so large experiments can consume RAM instead of disk. Use another
location for large-scale runs.

## Related

- Distributed workflow: [distributed.md](./distributed.md)
- First experiment: [../../getting-started/first-experiment.md](../../getting-started/first-experiment.md)
