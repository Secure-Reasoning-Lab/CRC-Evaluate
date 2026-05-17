# CRSBench - Cyber Reasoning System Benchmark Suite

CRSBench is the benchmark suite for [OSS-CRS](https://github.com/ossf/oss-crs),
the open-source orchestration framework for LLM-based autonomous bug-finding
and bug-fixing systems (Cyber Reasoning Systems). It provides curated
benchmarks and an evaluation harness for measuring any OSS-CRS-compatible CRS
on vulnerability discovery and program repair.

Unlike traditional fuzzing benchmarks (e.g., FuzzBench) that only report
coverage/crashes, CRSBench stores complete ground truth to track whether
vulnerabilities are actually found and correctly patched.

## Benchmark Statistics

The released benchmark suite contains:

| Metric | Value |
| --- | --- |
| Benchmarks | **124** (87 Delta + 37 Full) |
| Upstream projects | **82** |
| Vulnerabilities (CPVs) | **315** |
| C / C++ | 63 benchmarks, 123 vulnerabilities |
| JVM (Java) | 61 benchmarks, 192 vulnerabilities |
| Distinct CWEs | **91** (covers 21 of the 2025 CWE Top 25) |
| Vulnerabilities per harness | 1.65 average, 12 max |
| PoV variants per vulnerability | 3.89 average |

See [docs/reference/benchmark-statistics.md](docs/reference/benchmark-statistics.md)
for the full breakdown (origins, CWE distribution, and how to regenerate the
numbers from the shipped benchmarks).

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
  benchmark_suite: smoke/sanity
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
[docs/deployment/distributed.md](docs/deployment/distributed.md).

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

See [Distributed Experiments](docs/deployment/distributed.md) for multi-machine setup, core pinning, and
production deployment. See [Configuration](docs/getting-started/configuration.md) for `.env` configuration.

### CRS Config Resolution

CRS services are declared under `crs_compose` in experiment YAML. Each key
resolves to a registry entry under `./oss-crs/registry/<crs>.yaml` that
defines source and runtime defaults. For the full CRS interface and registry
schema, see the [OSS-CRS documentation](https://github.com/ossf/oss-crs).

LiteLLM credentials required for `runtime.litellm.mode: external` are
documented in [docs/getting-started/configuration.md](docs/getting-started/configuration.md).

For shell tab-completion setup, see
[docs/getting-started/install.md#shell-completion](docs/getting-started/install.md#shell-completion).

For standalone `verify`, `patch-verify`, and `coverage` usage, see
[docs/reference/standalone-verification.md](docs/reference/standalone-verification.md).

### Results

```bash
uv run crsbench report    --experiment my-exp
uv run crsbench dashboard --base-dir ./experiments  # experimental
uv run crsbench benchmark stats --output stats.csv
uv run crsbench benchmark stats --benchmark-suite crsbench-all --output stats.csv
uv run crsbench benchmark stats --benchmark-suite crsbench-all --vuln-index-output vuln-index.yaml
```

> **Note:** `crsbench dashboard` is an experimental Next.js viewer for
> already-generated reports. Routes, JSON contracts, and CLI flags may change
> without notice; it binds to `localhost` only. See
> [`dashboard/README.md`](dashboard/README.md) for details.

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
- Setup and runtime:
  - [Install](docs/getting-started/install.md)
  - [Configuration](docs/getting-started/configuration.md)
  - [Environment Variables](docs/reference/environment-variables.md)
  - [First Experiment](docs/getting-started/first-experiment.md)
  - [Distributed Experiments](docs/deployment/distributed.md)
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
