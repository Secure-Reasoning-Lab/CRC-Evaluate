# CRSBench - Cyber Reasoning System Benchmark Suite

A benchmark suite for evaluating AI-powered Cyber Reasoning Systems (CRS) across vulnerability discovery, program repair, and evaluation.

Unlike traditional fuzzing benchmarks (e.g., FuzzBench) that only report coverage/crashes, CRSBench stores complete ground truth to track whether vulnerabilities are actually found and correctly patched.

## Quick Start

First-time users should start with [docs/getting-started/first-experiment.md](docs/getting-started/first-experiment.md).

```bash
# Install
git clone https://github.com/sslab-gatech/CRSBench.git && cd CRSBench
uv sync
./scripts/setup-third-party.sh             # clone managed oss-fuzz + Atlantis checkouts
uv run crsbench prepare                    # pull OSS-Fuzz + AIxCC base images
uv run crsbench prepare --coverage         # pull Atlantis GHCR coverage images (or build locally as fallback)

# Download benchmarks from HuggingFace (gated — requires access)
#   1. Create a token at https://huggingface.co/settings/tokens
#   2. Accept the Data Use Agreement at https://huggingface.co/datasets/sslab-gatech/crsbench-dataset
#   3. Log in:
uv run hf auth login
crsbench download --all                     # all 134 benchmarks (~12GB)
crsbench download --all --no-ground-truth   # skip .aixcc/ ground truth
crsbench download --benchmark-suite sanity  # small test suite
```

Production-style single-machine distributed example (128 cores, AFC bugfixing):

```bash
uv run python scripts/valkey-helper.py start

# Terminal 1: orchestrator
uv run crsbench run \
  --experiment-config experiment-configs/afc-final-bugfixing/crs-codex-gpt-5-4-full.yaml \
  --distributed

# Terminal 2: configless worker
uv run crsbench worker \
  --jobs 12 \
  --cores-per-job 8 \
  --cpuset 0-95

# Terminal 3: configless evaluator (optional; use for real-time build/verify processing)
uv run crsbench evaluator \
  --jobs 4 \
  --cores-per-job 8 \
  --cpuset 96-127

# Clean stale queue state for one experiment (use the `experiment:` name from config)
uv run crsbench queue clean --experiment afc-all-crs-codex-gpt-5-4-full --yes
```

Notes:
- In this example, `run --distributed` registers experiment metadata in Redis, and the configless worker/evaluator pick up queue and resource requirements from that registry.
- For `experiment-configs/afc-final-bugfixing/crs-codex-gpt-5-4-full.yaml`, `resources.cores_per_trial` is `8`, so the worker should use `--cores-per-job 8`.
- `crsbench worker` is continuous by default. Use `--no-continuous` only when you want it to exit after the current backlog drains.
- `crsbench evaluator --jobs N --cores-per-job M` is the primary CLI. Split `--build-*` / `--verify-*` flags remain available only for advanced asymmetric setups.
- Add `--queue-mode fresh` to the `run` command when you want a clean rerun and do not want to resume stale queued or started jobs.

`crsbench prepare` typical duration:
- warm cache: ~10-60s
- first run (image pulls): ~3-15m
- with `--build-base-images`: 20m+ (can be significantly longer)

`crsbench prepare --coverage` prepares the separate Atlantis/given_fuzzer
coverage pipeline used by `crsbench coverage`. It reads the checkout from
`third_party/atlantis-multilang-given_fuzzer`, prefers the published Team
Atlanta GHCR images, and falls back to local `oss-crs prepare` only when those
images are unavailable.

If your virtual environment is not activated, prefix CLI commands with `uv run`
(for example, `uv run crsbench download --all`).

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
crsbench verify       benchmarks/project --pov-dir ./povs/
crsbench patch-verify benchmarks/project --patch-dir ./patches --pov-dir ./povs
crsbench coverage     benchmarks/project --corpus-dir ./corpus/  # experimental
crsbench coverage     --experiment-config ./experiment.yaml      # seed coverage over time
crsbench coverage     --experiment-dir ./experiment-output       # seed coverage over time
crsbench coverage     --seed-dir ./seeds --benchmark project --harness fuzz_target --output-dir ./coverage-out
```

Timeline coverage mode persists raw per-seed artifacts under the target
coverage directory's `raw/` subdirectory. Each analyzed seed keeps its
normalized `.cov` result and any captured crash log alongside the JSON/CSV/PNG
timeline outputs.

### Results

```bash
crsbench report    --experiment my-exp
crsbench dashboard --base-dir ./experiments
crsbench benchmark stats --output stats.csv
```

### Dataset

```bash
crsbench download --all                                        # all benchmarks
crsbench download --all --no-ground-truth                      # skip .aixcc/ ground truth
crsbench download --dataset crsbench --benchmarks afc-curl-delta-01  # specific
crsbench download --benchmark-suite sanity                     # suite
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
│   ├── migration/           #   Format migration tools
│   ├── hint_generation/     #   Progressive hint generation
│   ├── reporting/           #   Reports & dashboard
│   ├── statistics/          #   Benchmark statistics
│   └── utils/               #   Shared utilities (logger, YAML, etc.)
├── oss-crs/registry/        # OSS-CRS registry entries referenced by `crs_compose` keys
├── oss-crs/                 # CRS runtime and registry (submodule)
├── third_party/oss-fuzz/    # Official OSS-Fuzz (sparse checkout, managed by `crsbench prepare`)
├── third_party/patches/     # Local upstream patch sets consumed during prepare/build
├── docs/                    # Documentation hub (user + design docs)
└── docs/design/             # Internal architecture docs
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
- Architecture and modules:
  - [Design Docs](docs/design/README.md)
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
