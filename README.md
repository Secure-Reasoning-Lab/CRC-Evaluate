# CRSBench - Cyber Reasoning System Benchmark Suite

A benchmark suite for evaluating AI-powered Cyber Reasoning Systems (CRS) across vulnerability discovery, program repair, and evaluation.

Unlike traditional fuzzing benchmarks (e.g., FuzzBench) that only report coverage/crashes, CRSBench stores complete ground truth to track whether vulnerabilities are actually found and correctly patched.

## Quick Start

First-time users should start with the smallest local queue-backed run. The
commands below install CRSBench, prepare the managed dependencies, download the
small `sanity` benchmark suite, and then run one experiment with a local worker.
CRSBench is supported on Linux hosts only; the public quick start is intended
for a Linux machine or VM with Docker available.

```bash
git clone https://github.com/sslab-gatech/CRSBench.git && cd CRSBench
uv sync
./scripts/setup-third-party.sh
uv run crsbench prepare
# Required by the bundled Atlantis given-fuzzer starter CRS and by coverage workflows.
uv run crsbench prepare --coverage

# Download benchmarks from HuggingFace (gated — requires access)
#   1. Create a token at https://huggingface.co/settings/tokens
#   2. Accept the Data Use Agreement at https://huggingface.co/datasets/sslab-gatech/crsbench-dataset
#   3. Log in:
uv run hf auth login
uv run crsbench download --benchmark-suite sanity
```

Create a first-run config at `first-run.yaml`:

Expected local resources for this sanity run: Linux, Docker, 4 or more CPU
cores, enough disk for Docker images plus the sanity benchmark data, and a run
window on the order of minutes after the initial image pulls.

```yaml
experiment:
  name: first-run
  task: bugfinding
  mode: full
  benchmark_suite: sanity
  sanitizers: [address]

runtime:
  trials: 1
  max_total_time: 3600
  build_timeout: 900
  run_timeout: 1800
  verify_timeout: 900
  redis_host: localhost:6379
  litellm:
    skip: true

storage:
  experiment_filestore: ./results/experiment-data
  report_filestore: ./results/report-data

crs_compose:
  atlantis-multilang-given_fuzzer:
    num_cores: 4
```

`atlantis-multilang-given_fuzzer` selects the bundled Atlantis multi-language
given-fuzzer CRS adapter. With `runtime.litellm.skip: true`, this starter run
does not require external LLM credentials.

Then run it:

```bash
uv run python scripts/valkey-helper.py start

# Terminal 1
uv run crsbench worker --experiment-config first-run.yaml

# Terminal 2
uv run crsbench run --experiment-config first-run.yaml
```

For a guided version of this flow, see
[docs/getting-started/first-experiment.md](docs/getting-started/first-experiment.md).
For multi-machine and production-style runs, see
[docs/guides/experiments/distributed.md](docs/guides/experiments/distributed.md).

`crsbench prepare` typical duration:
- warm cache: ~10-60s
- first run (image pulls): ~3-15m
- with RTS base image builds or `--build-base-images`: 20m+ (can be
  significantly longer)

Default `crsbench prepare` bootstraps the managed OSS-Fuzz checkout, pulls the
standard OSS-Fuzz and AIxCC base images, and then attempts to prebuild the RTS
base images used by RTS-enabled benchmarks. If RTS prebuild cannot complete,
`crsbench prepare` fails so a successful run still means the RTS image set is
ready in advance.

`crsbench prepare --coverage` prepares the Atlantis/given_fuzzer runtime used
by the starter CRS above and by `crsbench coverage`. It reads the checkout from
`third_party/atlantis-multilang-given_fuzzer`, prefers the published Team
Atlanta GHCR `1.0.0` images, retags them onto the canonical local Atlantis
image names, and falls back to local `oss-crs prepare` only when those images
are unavailable.

If your virtual environment is activated, you may omit the `uv run` prefix.

For experiment config authoring, CRSBench provides both
`crsbench gen-config` for the prompt-driven wizard and
`crsbench gen-config-tui` for the full-screen Textual editor.

CRSBench supports queue-backed execution with Redis/RQ. In that model, the
orchestrator (`run`) enqueues jobs, workers (`worker`) execute CRS trial jobs,
and `evaluator` processes build/verify queues for real-time POV and patch
verification. For the smallest first run, follow
[docs/getting-started/first-experiment.md](docs/getting-started/first-experiment.md).

### Run / Worker / Evaluator Workflow

```text
Machine A (orchestrator + Redis)
┌──────────────────────────┐
│ crsbench run             │
│ (orchestrator)           │
└─────────────┬────────────┘
              │ enqueue jobs + metadata
              v
      ┌───────────────────┐
      │ Redis / RQ queues │
      └───────┬─────┬─────┘
              │     │
   build/run  │     │ verify/patch-verify/coverage
              │     │
   ┌──────────┘     └──────────┐
   v                           v
Machine B/C/...               Machine D (single evaluator)
┌─────────────────────┐       ┌──────────────────────┐
│ crsbench worker     │  ...  │ crsbench evaluator   │
│ executes CRS jobs   │       │ executes verify jobs │
└──────────┬──────────┘       └──────────┬───────────┘
           │                             │
           └────── writes artifacts/logs ┴───────>
                 shared experiment/trial output dirs
```

- Non-interactive runs default to scoped `continue` when existing jobs are detected.
- Retry failed trials only when explicitly requested:
  `crsbench run --experiment-config ... --queue-mode continue --retry-failed`

### Execution Notes (oss-crs Workflow)

- CRS lifecycle now uses `oss-crs prepare`, `oss-crs build-target`, `oss-crs artifacts`, and `oss-crs run`
- Trial artifact discovery is resolved via `oss-crs artifacts` (no glob-based submit-dir discovery)
- Real-time POV/patch collection is tied to resolved `EXCHANGE_DIR` paths
- Additional POV dedup strategy `stack-based` is available via `pov_dedup_strategy` in experiment config

See [Distributed Experiments](docs/guides/experiments/distributed.md) for multi-machine setup, core pinning, and
production deployment. See [Configuration](docs/getting-started/configuration.md) for `.env` configuration.

### CRS Config Resolution (Important)

CRS services are declared under `crs_compose` in experiment YAML.

Resolution flow:
1. Add one or more CRS service keys under `crs_compose` (for example `crs-codex`)
2. Each CRS key resolves to `./oss-crs/registry/<crs>.yaml` by default
3. Registry YAML defines source (git or `local_path`) and runtime defaults

Example:

```yaml
crs_compose:
  crs-codex:
    num_cores: 8
```

For LiteLLM in experiment runtime (`runtime.litellm.mode: external`):
- Tracking enabled (`runtime.litellm.tracking_enabled: true`) requires `CRSBENCH_LLM_UPSTREAM_MASTER_KEY`
- Tracking disabled (`runtime.litellm.tracking_enabled: false`) requires either `CRSBENCH_LLM_UPSTREAM_API_KEY` or `CRSBENCH_LLM_UPSTREAM_MASTER_KEY`

### Shell Completion

Enable tab-completion for all `crsbench` subcommands and options:

```bash
# Bash
activate-global-python-argcomplete --user   # then restart shell or source ~/.bashrc

# Zsh — add to ~/.zshrc
eval "$(register-python-argcomplete crsbench)"
```

See the [argcomplete docs](https://github.com/kislyuk/argcomplete#installation) for
fish and other shell setup instructions.

### Verification (Standalone)

```bash
uv run crsbench verify       benchmarks/project --pov-dir ./povs/ --jobs 4 --cores-per-job 2
uv run crsbench patch-verify benchmarks/project --patch-dir ./patches --pov-dir ./povs --jobs 4 --cores-per-job 2
uv run crsbench coverage     --experiment-config ./experiment.yaml      # seed coverage over time
uv run crsbench coverage     --experiment-dir ./experiment-filestore/experiment-name       # seed coverage over time
uv run crsbench coverage     --experiment-dir ./experiment-filestore/experiment-name --output-dir ./coverage-out
uv run crsbench coverage     --seed-dir ./seeds --benchmark project --harness fuzz_target --output-dir ./coverage-out
uv run crsbench coverage     --seed-dir ./seeds --experiment-start-time 1710000000 --benchmark project --harness fuzz_target --output-dir ./coverage-out
```

For standalone `verify` / `patch-verify`, `--jobs` and `--cores-per-job`
are the primary parallelism flags. Legacy `--build-workers` and
`--verify-workers` remain accepted as hidden compatibility aliases.

Timeline coverage mode persists raw per-seed artifacts under the target
coverage directory's `raw/` subdirectory. Each analyzed seed keeps its
normalized `.cov` result and any captured crash log alongside the JSON/CSV/PNG
timeline outputs. `coverage_timeline.json` stores one row per normalized seed,
`coverage_timeline.csv` emits one row per normalized seed, and
`coverage_timeline.png` plots cumulative covered lines directly from those
per-seed replay results. Direct `--seed-dir` mode derives relative time from
each input seed file's original `mtime` using the first retained seed as the
origin, unless `--experiment-start-time` is supplied to override the origin
with an explicit Unix timestamp. `--experiment-dir` and `--experiment-config` instead use
`povs/pov_store.json.crs_run_start_time` as the origin and clamp the x-axis to
the recorded trial `run_time` from `metadata.json`. When `--output-dir` is
supplied for experiment-backed coverage, CRSBench mirrors the experiment under
`<output-dir>/<experiment-name>/.../trial-N/coverage`; otherwise it writes to
each source trial's in-place `coverage/` directory. The Atlantis timeline path
does not run a separate whole-corpus denominator pass, so total-line
percentages may be reported as unavailable.
Coverage analysis uses the Atlantis/given_fuzzer warm-runner backend and does
not accept an `--oss-fuzz-path` override.

### Results

```bash
uv run crsbench report    --experiment my-exp
uv run crsbench dashboard --base-dir ./experiments
uv run crsbench benchmark stats --output stats.csv
uv run crsbench benchmark stats --benchmark-suite crsbench-all --output stats.csv
uv run crsbench benchmark stats --benchmark-suite crsbench-all --vuln-index-output vuln-index.yaml
```

### Dataset

```bash
uv run crsbench download --all                                        # all benchmarks
uv run crsbench download --all --no-ground-truth                      # skip .aixcc/ ground truth
uv run crsbench download --dataset crsbench --benchmarks afc-curl-delta-01  # specific
uv run crsbench download --benchmark-suite sanity                     # suite
```

Each benchmark is stored as two tarballs: `benchmark.tar.gz` (project files, build scripts,
source packages) and `ground-truth.tar.gz` (`.aixcc/` vulnerability metadata and patches).
Use `--no-ground-truth` for blind CRS evaluation without vulnerability answers.

See [CONTRIBUTING.md](CONTRIBUTING.md) for developer commands (`crsbench benchmark`).

## Architecture

```
CRSBench/
├── benchmarks/              # Benchmark projects (RFC format)
├── crsbench/                # Main Python package
│   ├── run_experiment.py    #   CLI entry point
│   ├── builder/             #   OSS-Fuzz variant building
│   ├── evaluation/          #   CRS execution & verification
│   ├── benchmark_ci/        #   Benchmark CI pipeline
│   ├── distributed/         #   Multi-machine execution (Redis/RQ)
│   ├── benchmark/           #   Packaging, canary, seed tools
│   ├── dataset/             #   HuggingFace upload/download
│   ├── validation/          #   Format validation & schemas
│   ├── hint_generation/     #   Progressive hint generation
│   ├── reporting/           #   Reports & dashboard
│   ├── statistics/          #   Benchmark statistics
│   └── utils/               #   Shared utilities (logger, YAML, etc.)
├── oss-crs/registry/        # OSS-CRS registry entries referenced by `crs_compose` keys
├── oss-crs/                 # CRS runtime and registry (submodule)
├── third_party/oss-fuzz/    # Official OSS-Fuzz (sparse checkout, managed by `crsbench prepare`)
├── third_party/patches/     # Local upstream patch sets consumed during prepare/build
└── docs/                    # Documentation hub
```

## Documentation

- Entry point: [docs/README.md](docs/README.md)
- Benchmark format contract: [docs/RFC.md](docs/RFC.md)
- Documentation governance:
  - [Taxonomy and Canonical Map](docs/governance/documentation-taxonomy.md)
  - [Inventory and Audit](docs/governance/documentation-inventory.md)
- Setup and runtime:
  - [Install](docs/getting-started/install.md)
  - [Configuration](docs/getting-started/configuration.md)
  - [Environment Variables](docs/reference/environment-variables.md)
  - [First Experiment](docs/getting-started/first-experiment.md)
  - [Distributed Experiments](docs/guides/experiments/distributed.md)
  - [Distributed Experiment Config Contract (source-of-truth)](docs/experiment-config-distributed-example.yaml)
- Contributor tracks:
  - [Framework Developer Guide](docs/contributors/framework-developer-guide.md)
  - [Benchmark Developer Guide](docs/contributors/benchmark-developer-guide.md)
- Module reference:
  - [Module Docs](docs/modules/README.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and coding standards.

## License

CRSBench is licensed under [MIT](LICENSE). Bundled upstream source code retains
its original license — see [LICENSE-THIRD-PARTY.md](LICENSE-THIRD-PARTY.md).

## Related Projects

- [FuzzBench](https://github.com/google/fuzzbench) - Fuzzer evaluation platform
- [OSS-Fuzz](https://github.com/google/oss-fuzz) - Continuous fuzzing for open source
- [AIxCC](https://aicyberchallenge.com/) - AI Cyber Challenge
