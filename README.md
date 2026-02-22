# CRSBench - Cyber Reasoning System Benchmark Suite

A benchmark suite for evaluating AI-powered Cyber Reasoning Systems (CRS) across vulnerability discovery, program repair, and evaluation.

Unlike traditional fuzzing benchmarks (e.g., FuzzBench) that only report coverage/crashes, CRSBench stores complete ground truth to track whether vulnerabilities are actually found and correctly patched.

## Quick Start

```bash
# Install
git clone https://github.com/sslab-gatech/CRSBench.git && cd CRSBench
uv sync

# Download benchmarks from HuggingFace (gated — requires access)
#   1. Create a token at https://huggingface.co/settings/tokens
#   2. Accept the Data Use Agreement at https://huggingface.co/datasets/sslab-gatech/crsbench-dataset
#   3. Log in:
uv run hf auth login
crsbench download --all                     # all 134 benchmarks (~12GB)
crsbench download --all --no-ground-truth   # skip .aixcc/ ground truth
crsbench download --benchmark-suite sanity  # small test suite

# Run an experiment (requires Valkey/Redis for job queue)
python scripts/valkey-helper.py --password start  # auto-generates password, saved to .env
crsbench run       --experiment-config experiment-configs/experiment-config-sanity.yaml
crsbench worker    --experiment-config experiment-configs/experiment-config-sanity.yaml
crsbench evaluator --experiment-config experiment-configs/experiment-config-sanity.yaml
```

If your virtual environment is not activated, prefix CLI commands with `uv run`
(for example, `uv run crsbench download --all`).

CRS experiments use a distributed job queue (Redis/RQ). The orchestrator (`run`) enqueues jobs,
workers (`worker`) execute them. Add `evaluator` for real-time POV and patch verification.

### Execution Notes (oss-crs Workflow)

- CRS lifecycle now uses `oss-crs prepare`, `oss-crs build-target`, `oss-crs artifacts`, and `oss-crs run`
- Trial artifact discovery is resolved via `oss-crs artifacts` (no glob-based submit-dir discovery)
- Real-time POV/patch collection is tied to resolved `EXCHANGE_DIR` paths
- Additional POV dedup strategy `stack-based` is available via `pov_dedup_strategy` in experiment config

See [Experiment Workflow](docs/experiment-workflow.md) for multi-machine setup, core pinning, and
production deployment. See [Environment Setup](docs/environment-setup.md) for `.env` configuration.

### Verification (Standalone)

```bash
crsbench verify       benchmarks/project --pov-dir ./povs/
crsbench patch-verify benchmarks/project --patch-dir ./patches --pov-dir ./povs
crsbench coverage     benchmarks/project --corpus-dir ./corpus/
```

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
├── crses/                   # CRS configurations for evaluation
├── oss-crs/                 # CRS runtime and registry (submodule)
├── oss-fuzz/                # OSS-Fuzz (submodule)
├── docs/                    # Documentation hub (user + design docs)
└── docs/design/             # Internal architecture docs
```

## Documentation

- Entry point: [docs/README.md](docs/README.md)
- Benchmark format contract: [docs/RFC.md](docs/RFC.md)
- Documentation governance:
  - [Taxonomy and Canonical Map](docs/documentation-taxonomy.md)
  - [Inventory and Audit](docs/documentation-inventory.md)
  - [Maintenance Guide](docs/documentation-maintenance.md)
- Setup and runtime:
  - [Environment Setup](docs/environment-setup.md)
  - [Environment Variables](docs/environment-variables.md)
  - [Experiment Workflow](docs/experiment-workflow.md)
- Contributor tracks:
  - [Framework Developer Guide](docs/framework-developer-guide.md)
  - [Benchmark Developer Guide](docs/benchmark-developer-guide.md)
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
