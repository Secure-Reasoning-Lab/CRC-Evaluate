# Discovery-Only OSS-Fuzz Experiments

Use this workflow when you want to run CRSBench against OSS-Fuzz-format
projects that do not have CRSBench ground truth under `.aixcc/`.

This mode is intended for discovery and improvement pipelines on arbitrary
OSS-Fuzz projects:

- benchmark directories come from an OSS-Fuzz `projects/` tree
- project language comes from `project.yaml` and is not limited to C/C++ or JVM
- CRSBench generates a minimal `.aixcc/meta.yaml` by discovering fuzz targets
- harnesses without CPVs are still scheduled when
  `experiment.only_cpv_harnesses: false`

## Prerequisites

- The target project directory contains `project.yaml`, `Dockerfile`, and
  `build.sh`.
- `oss_fuzz_path` points at an OSS-Fuzz checkout.
- `benchmarks_root` points at the directory containing the project
  subdirectories, typically a separate OSS-Fuzz `projects/` checkout or mirror
  such as `/path/to/oss-fuzz/projects`.
- Docker is available locally, because `crsbench benchmark init` builds the
  target project image and runs the project build to discover fuzzers.

Important:

- The managed `third_party/oss-fuzz` checkout in this repository is sparse and
  is used as the helper/build-output checkout.
- It does not populate `projects/<name>/` by default, so `benchmarks_root`
  usually needs to point at a separate OSS-Fuzz projects checkout.
- Managed cloud launches can use `benchmarks_root: third_party/oss-fuzz/projects`
  for raw OSS-Fuzz project names. VM bootstrap materializes the selected
  `projects/<name>/` directories from the managed checkout and generates
  `.aixcc/meta.yaml` automatically on each VM before the run starts.

## Minimal Config

Use `full` mode for these benchmarks. `crsbench benchmark init` generates
`full_mode` metadata only.

```yaml
experiment:
  name: discovery-libyang
  task: bugfinding
  mode: full
  benchmarks:
    - libyang
  only_cpv_harnesses: false
  sanitizers:
    - address

benchmarks_root: /path/to/oss-fuzz/projects
oss_fuzz_path: third_party/oss-fuzz

runtime:
  trials: 1
  max_total_time: 3600
  build_timeout: 3600
  run_timeout: 600
  verify_timeout: 60
  skip_verification: true
  source_mode: main_repo
  redis_host: localhost

storage:
  experiment_filestore: /tmp/discovery-exp
  report_filestore: /tmp/discovery-report

crs_compose:
  atlantis-multilang-given_fuzzer:
    num_cores: 8

resources:
  cores_per_trial: 8
  memory_per_trial: "32G"
```

Key settings:

- `experiment.only_cpv_harnesses: false` keeps harnesses even when there are no
  CPVs in metadata.
- `runtime.skip_verification: true` is recommended because there is no
  benchmark ground truth to verify against.
- `runtime.source_mode: main_repo` is recommended for raw OSS-Fuzz projects,
  because their sources usually come from `project.yaml:main_repo` rather than
  CRSBench-style `pkgs/` tarballs.
- `benchmarks_root` and `oss_fuzz_path` must match the checkout you want to run
  against.

Repository examples:

- [`experiment-configs/discovery-testing/oss-fuzz-given-fuzzer-8core.yaml`](../../../experiment-configs/discovery-testing/oss-fuzz-given-fuzzer-8core.yaml)
- [`experiment-configs/discovery-testing/atlantis-multilang-wo-concolic-full-10min-5usd.yaml`](../../../experiment-configs/discovery-testing/atlantis-multilang-wo-concolic-full-10min-5usd.yaml)
- [`experiment-configs/discovery-smoke-testing/opencode-go-yaml-bugfinding.yaml`](../../../experiment-configs/discovery-smoke-testing/opencode-go-yaml-bugfinding.yaml)
- [`experiment-configs/discovery-smoke-testing/gce-opencode-go-yaml-bugfinding.yaml`](../../../experiment-configs/discovery-smoke-testing/gce-opencode-go-yaml-bugfinding.yaml)

## Initialize The Benchmarks

Run initialization before `crsbench run` for local or manually managed worker
setups:

```bash
uv run crsbench benchmark init --experiment-config config.yaml
```

To pin the discovery build to specific CPUs:

```bash
uv run crsbench benchmark init \
  --experiment-config config.yaml \
  --cpuset-cpus 0-7
```

To initialize multiple benchmarks in parallel while keeping each OSS-Fuzz build
inside a disjoint CPU slice:

```bash
uv run crsbench benchmark init \
  --experiment-config config.yaml \
  --jobs 4 \
  --cpuset-cpus 0-31
```

`--jobs` parallelizes benchmark initialization across multiple OSS-Fuzz
projects. When `--cpuset-cpus` is also set and `--jobs > 1`, CRSBench treats
that cpuset as the total CPU envelope for the init command and splits it into
per-job slices before calling the underlying OSS-Fuzz build path. Without an
explicit cpuset, parallel init uses the current process affinity envelope.

`benchmark init` does the following for each selected benchmark:

- checks that the project looks like an OSS-Fuzz project
- builds the project and discovers fuzz target binaries
- writes `.aixcc/meta.yaml` with discovered `harness_files`
- records a `full_mode.base_commit` from `project.yaml:main_repo`

If `.aixcc/meta.yaml` already exists, `benchmark init` skips that benchmark.

Managed cloud launches using
`benchmarks_root: third_party/oss-fuzz/projects` do this automatically during
VM bootstrap for raw OSS-Fuzz project names, so you do not need a separate
pre-initialized benchmark checkout on the VMs. When cloud worker/evaluator
sizing is configured, bootstrap reuses that sizing to initialize multiple
OSS-Fuzz benchmarks in parallel under disjoint CPU slices instead of building
them strictly one by one.

## Run The Experiment

After initialization, run the experiment normally:

```bash
uv run crsbench run --experiment-config config.yaml
```

For the smallest one-machine validation without Valkey or a worker:

```bash
uv run crsbench run --experiment-config config.yaml --local-only
```

The same initialized benchmarks can also be used with the distributed worker
and evaluator workflows documented in [Distributed](./distributed.md).

For a managed GCE smoke example pinned to `us-central1`, see
[`gce-opencode-go-yaml-bugfinding.yaml`](../../../experiment-configs/discovery-smoke-testing/gce-opencode-go-yaml-bugfinding.yaml).

## Limits And Expectations

- `crsbench run` expects `.aixcc/meta.yaml` to already exist for these
  benchmarks. The initialization step is explicit; it is not created during the
  run command.
- Discovery initialization creates `full_mode` metadata only. If
  `experiment.mode: delta` is set, these benchmarks will not be eligible.
- If `project.yaml:main_repo` cannot be resolved to a real HEAD commit,
  initialization can fall back to a placeholder commit in `.aixcc/meta.yaml`.
  Update that commit before running, or source checkout will fail later.
- Discovery-only runs collect raw POV and patch artifacts, but with
  `skip_verification: true` they are not scored against benchmark ground truth.
- `only_cpv_harnesses: false` applies to both bug-finding and bug-fixing CRS
  types. For bug-fixing workflows, you still need a POV source such as a prior
  finding experiment.
- Discovery-mode initialization and trial orchestration work with arbitrary
  OSS-Fuzz benchmark languages such as C/C++, JVM, Go, Rust, and Python, as
  long as the selected CRS can run that target language.
- Coverage and RTS remain language-specific subsystems. Coverage currently
  supports C/C++ and JVM only; if enabled for an unsupported benchmark
  language, CRSBench skips coverage for that benchmark with a warning instead
  of failing the discovery run.
